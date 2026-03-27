#!/usr/bin/env python3


# Copyright 2020-2021 University of Utah

#
# Measurements client
#

import logging
import time
import multiprocessing as mp
import argparse

import numpy as np
import scipy.signal as sig
import daemon

import measurements_pb2 as measpb
from clientconnector import ClientConnector
from dsss.goldutils import *
from radio import Radio
from rpccalls import *
from sigutils import *
from common import *

from utils import *

DEF_LOGFILE="/var/tmp/ccontroller.log"
DEF_IP = "127.0.0.1"

class MeasurementsClient:
    XMIT_SAMPS_MIN = 500000
    TOFF = 0.5
    SAMPLEOFF = 1024 # First couple of samples are misleading

    def __init__(self, args):
        self.npipe = None
        self.conproc = None
        self._setup_logger(args.logfile)
        self.connector = ClientConnector(args)
        if "cbrs" in self.connector.name or args.usetxrx: #To-do: check this
            usetxrx = True
        else:
            usetxrx = False
        if not args.noexternalclock and (
            "cbrs" in self.connector.name or 
            "cnode" in self.connector.name or 
            "cell" in self.connector.name or 
            args.useexternalclock
        ):
            useexternalclock = True
        else:
            useexternalclock = False
        self.gpiomode = None
        self.radio = Radio(args.args, usetxrx, useexternalclock)
        
        

    def _setup_logger(self, logfile):
        fmat = logging.Formatter(fmt=LOGFMAT, datefmt=LOGDATEFMAT)
        shandler = logging.StreamHandler()
        shandler.setFormatter(fmat)
        fhandler = logging.FileHandler(logfile)
        fhandler.setFormatter(fmat)
        self.logger = mp.get_logger()
        self.logger.setLevel(LOGLEVEL)
        self.logger.addHandler(shandler)
        self.logger.addHandler(fhandler)

    def _start_netproc(self):
        (n1, n2) = mp.Pipe()
        self.npipe = n1
        self.netproc = mp.Process(target=self.connector.run, args=(n2,))
        self.netproc.start()

    def _tune_radio(self, args):
        self.radio.tune(args['freq'], args['gain'], args['rate'], args['use_lo_offset'], self.gpiomode)

    def _xmit_samps(self, args, samples):
        self.radio.send_samples(samples)

    def _recv_samps(self, args):
        samps, dt = self.radio.recv_samples(args['nsamps'], args['start_time'] if 'start_time' in args else None)
        return samps[0], dt # TODO: check if samps[0] causes problem for other functions or not

    def _do_meas_power(self, args, msg, rmsg):
        foff = args['filter_bw']/2
        flo, fhi = args['wfreq']-foff, args['wfreq']+foff
        self.logger.info("Sampling power between %f and %f" % (args['freq'] + flo, args['freq'] + fhi))
        samps, dt = self._recv_samps(args)
        fsamps = butter_filt(samps, flo, fhi, args['rate'])
        rmsg.measurements.append(get_avg_power(fsamps))
        print('Power: ', get_avg_power(fsamps))
        if args['get_samples']:
            encode_samples(rmsg, samps)
        
    def master_recv_samps(self, args, msg, rmsg):
        if args['gold_id'] >= 0:
            self._do_recv_gold_codes(args, msg, rmsg)
        else:
            self._do_recv_samps(args, msg, rmsg)

    def _do_recv_samps(self, args, msg, rmsg):
        """
        Collect IQ samples, with an optional band-pass filter
        """
        # Set RX params
        args['nsamps'] = args['nsamps'] + self.SAMPLEOFF
        if 'cnode' in self.connector.name:
            self.gpiomode = 0x10
        self._tune_radio(args)

        # Get samples and add to measurements
        samps, dt = self._recv_samps(args)
        if args['filter_bw'] > 0:
            self.logger.info("Sampling power between %f and %f" % (args['wfreq'] - args['filter_bw'], args['wfreq'] + args['filter_bw']))
            samps = butter_filt_with_shift(samps, args['wfreq'], args['filter_bw'], args['rate'])
        
        add_to_measurements(rmsg.measurements, dt)

        # Set return samples
        encode_samples(rmsg, samps[self.SAMPLEOFF:])
    
    def _do_recv_gold_codes(self, args, msg, rmsg):
        """
        Collect raw IQ samples or demodulated signal from a transmitted gold_code, optionally 
        calculating stats such as SNR, BER, and signal power.
        """
        # Set RX params
        tx_rate = args['rate']
        gold_length = args['gold_length'] # spread factor / process gain
        gold_id = args['gold_id']
        samps_per_chip = args['samps_per_chip'] 
        chip_rate = tx_rate / samps_per_chip
        symbol_rate = chip_rate  / gold_length
        use_pulse_shaping = 'use_pulse_shaping' in args and args['use_pulse_shaping']

        if args['nsymbols']:
            args['nsamps'] = int((args['nsymbols']+1) * samps_per_chip * gold_length + self.SAMPLEOFF)
            nsymbols = args['nsymbols']
        else:
            args['nsamps'] = args['nsamps'] + self.SAMPLEOFF
        if 'cnode' in self.connector.name:
            self.gpiomode = 0x10
        self._tune_radio(args)
        
        gold_code = get_gold_code(gold_length, gold_id)  * 2 - 1
        long_gold_code = np.repeat(gold_code, samps_per_chip)

        on_client_repeat = max(args['on_client_repeat'], 1)
        for repeat_ind in range(on_client_repeat):
            samps, dt = self._recv_samps(args)
            samps = samps[self.SAMPLEOFF:]

            ## TODO: bandpass filter?
            if args['filter_bw'] > 0:
                samps = butter_filt_with_shift(samps, 0, args['filter_bw'], args['rate'])

            add_to_measurements(rmsg.measurements, dt)
            #add_attr(rmsg, 'timestamp' + str(repeat_ind) if 'on_client_repeat' > 1 else '', dt)

            if args['return_iq']:
                # Our sample buffer on the measiface side is a specific size, so I'm sending all the samples
                encode_samples(rmsg, samps)
            else:
                if use_pulse_shaping:
                    # matched filtering
                    pulse = SRRC(0.5, samps_per_chip, 5)
                    MFsamps = sig.lfilter(pulse, 1, samps.real) + 1j* \
                             sig.lfilter(pulse, 1, samps.imag)
                    # Coarse Frequency Offset Sync
                    FreqsyncSamp = Freq_Offset_Correction(MFsamps, tx_rate, order=2, debug=False)
                    # Clock Synchronization
                    if not args['sync']:
                        FreqsyncSamp = clock_sync(FreqsyncSamp, tx_rate, interpo_factor=16)
                    # Fine Freq Synchronization
                    FreqsyncSamp1, _, _, _ = DD_carrier_sync(FreqsyncSamp, 2, 0.01)
                    # Despreading with gold code
                    corr = sig.correlate(FreqsyncSamp1, long_gold_code)[len(long_gold_code)//2-1:-(len(long_gold_code)//2)]
                else:
                    corr = sig.correlate(samps, long_gold_code)

                # # Symbol Samples: method 1
                # corr_abs = abs(corr)
                # ind = np.argmax(corr_abs[:int(len(long_gold_code)*1.5)])
                # peak_locs = np.arange(len(corr_abs))[ind::len(long_gold_code)]

                # Symbol Samples: method 2
                corr_abs = abs(corr)
                hmin = (np.mean(corr_abs)+corr_abs.max())/2
                peak_locs = sig.find_peaks(corr_abs, distance=len(long_gold_code)-4, height=hmin)[0]
                
                # # avoid interference via peak value upper bound
                # if len(peak_locs)<len(simple_bits):
                #     hmax = np.mean(corr_abs) + 3*np.std(corr_abs)
                #     peak_locs = sig.find_peaks(corr_abs, distance=len(long_gold_code)-4, height=[hmin,hmax])[0]

                if args['return_stats']:
                    stats = get_gold_stats(corr, peak_locs)
                    encode_samples(rmsg, np.array(list(stats.values())))
                else:
                    encode_samples(rmsg, corr)


    def _do_rssi(self, args, msg, rmsg):
        """
        Collect RSSI values, with optional band-pass filter
        """
        # Set RX params
        args['nsamps'] = args['nsamps'] + self.SAMPLEOFF
        if 'cnode' in self.connector.name:
            self.gpiomode = 0x10
        self._tune_radio(args)

        # Get samples and add to measurements
        samps, dt = self._recv_samps(args)
        
        if args['filter_bw'] > 0:
            self.logger.info("Sampling power between %f and %f" % (args['wfreq'] - args['filter_bw'], args['wfreq'] + args['filter_bw']))
            samps = butter_filt_with_shift(samps, args['wfreq'], args['filter_bw'], args['rate'])
        
        add_to_measurements(rmsg.measurements, dt, get_avg_power(samps))


    def _do_xmit_sine(self, args, msg, rmsg):
        ratio = args['rate']/args['wfreq']
        nsamps = ratio * np.ceil(self.XMIT_SAMPS_MIN/ratio)
        sinebuf = mk_sine(int(nsamps), args['wampl'], args['wfreq'],
                          args['rate'])
        count = sinebuf.size
        while time.time() < args['end_time']:
            self._xmit_samps(args, sinebuf)
            #print(count/args['rate'],"sec")
            #count = count + sinebuf.size
    
    def _do_xmit_samps(self, args, msg, rmsg):
        """
        Transmit samples, from one of several sample sources:

        args['txfile']:  transmit samples from the specified file (see singal_library/)
        sine:  transmit a sine wave using args['sine_wfreq'] and args['sine_wampl'] parameters
        args['gold_id']: transmit BPSK-DSSS modulated samples
        """
        if 'cnode' in self.connector.name: 
            self.gpiomode = 0x00
        self._tune_radio(args)
        #print(args)
        if args['txfile']:
            samples = get_samps_frm_file(args['txfile'])
            self.logger.debug("Transmitting from " + args['txfile'])
        elif args['sine_wfreq'] and args['sine_wampl']:
            ratio = args['rate']/args['sine_wfreq']
            nsamps = ratio * np.ceil(self.XMIT_SAMPS_MIN/ratio)
            samples = mk_sine(int(nsamps), args['sine_wampl'], args['sine_wfreq'], args['rate'])
            self.logger.debug("Transmitting sine")
        elif args['gold_id'] < 0:
            self.logger.error("Unsupported tx samps: ", args)
            exit(1)

        if args['gold_id'] >= 0:
            tx_rate = args['rate']
            gold_length = args['gold_length'] # spread factor / process gain
            gold_id = args['gold_id']
            samps_per_chip = args['samps_per_chip'] 
            chip_rate = tx_rate / samps_per_chip
            symbol_rate = chip_rate  / gold_length
            use_pulse_shaping = 'use_pulse_shaping' in args and args['use_pulse_shaping']
            
            # Currently lookup, should switch to generation function (or precompute codes)
            gold_code = get_gold_code(gold_length, gold_id)  * 2 - 1
            if use_pulse_shaping:
                # up sampling
                long_gold_code = np.zeros(len(gold_code)*samps_per_chip)
                long_gold_code[::samps_per_chip] = gold_code
            else:
                long_gold_code = np.repeat(gold_code, samps_per_chip)

            # Samples are BPSK-DSSS modulated IQ values
            if 0 in simple_bits:
                # BPSK modulation
                samples = (simple_bits * 2 - 1) 
            # DSSS spreading
            repeated_gold_code = np.tile(long_gold_code, len(samples))
            samples = repeated_gold_code * np.repeat(samples, len(long_gold_code))
            
            if use_pulse_shaping:
                # SRRC pulse shape filter
                pulse = SRRC(0.5, samps_per_chip, 5)
                samples = np.convolve(samples, pulse, mode='same')[np.newaxis, :]
            else:
                samples = samples[np.newaxis, :]
            
        self.radio.xmit_samples(samples, args['duration'])
        self.logger.info('TX done!')
    

    def _do_seq(self, args, msg, rmsg, func):
        self.logger.info("Performing %s radio command sequence." % func.__code__.co_name)
        if 'cnode' in self.connector.name:
            if 'recv' in func.__code__.co_name or 'meas' in func.__code__.co_name or 'rssi' in func.__code__.co_name:
                self.gpiomode = 0x10
            else:
                self.gpiomode = 0x00
        self._tune_radio(args)
        steps = int(np.floor(args['rate']/args['freq_step']/2))
        if not args['start_time']:
            args['start_time'] = np.ceil(time.time())
        for i in range(1,steps):
            args['wfreq'] = i*args['freq_step']
            stime = args['start_time'] + (i-1)*args['time_step']
            etime = stime + args['time_step']
            args['end_time'] = etime - self.TOFF
            sltime = stime - time.time()
            if sltime > 0:
                time.sleep(sltime)
            else:
                self.logger.info("Late: %f" % sltime)
            self.logger.info("Seq %d: %0.2f/%d/%d" % (i, args['wfreq'], stime, etime))
            func(self, args, msg, rmsg) #TODO: check how to set radio type

    def echo_reply(self, args, msg, rmsg):
        self.logger.info("Received Echo Request. Sending response.")
        add_attr(rmsg, "type", "reply")

    def recv_samps(self, args, msg, rmsg):
        add_attr(rmsg, "rate", args['rate'])
        self.logger.info("Collecting %d samples." % args['nsamps'])
        if 'cnode' in self.connector.name:
            self.gpiomode = 0x10
        self._tune_radio(args)
        samps, dt = self._recv_samps(args)
        encode_samples(rmsg, samps)

    def xmit_samps(self, args, msg, rmsg):
        samps = decode_samples(msg)
        self.logger.info("Transmitting %d samples." % len(args['samples']))
        if 'cnode' in self.connector.name:
            self.gpiomode = 0x00
        self._tune_radio(args)
        self._xmit_samps(args, samps)
        add_attr(rmsg, "result", "done")

    def xmit_sine(self, args, msg, rmsg):
        self.logger.info("Sending sine wave with freq %f" % args['wfreq'])
        if 'cnode' in self.connector.name:
            self.gpiomode = 0x00
        self._tune_radio(args)
        args['end_time'] = time.time() + args['duration']
        self._do_xmit_sine(args, msg, rmsg)
        add_attr(rmsg, "result", "done")

    def meas_power(self, args, msg, rmsg):
        if 'cnode' in self.connector.name:
            self.gpiomode = 0x00
        self._tune_radio(args)
        self._do_meas_power(args, msg, rmsg)

    def run(self):
        self._start_netproc()
        self.radio.init_radio()

        while True:
            msg = measpb.SessionMsg()
            self.logger.debug("Waiting for command...")
            msg.ParseFromString(self.npipe.recv())
            #self.logger.error("Error in decoding msg.")
            func = get_function(msg)
            if func in self.CALLS:
                args = RPCCALLS[func].decode(msg)
                rmsg = mk_result(msg.uuid, self.connector.ptype, func)
                if func.startswith('seq'):
                    self.logger.debug("%s sequence start." % func)
                    self._do_seq(args, msg, rmsg, self.CALLS[func])
                    self.logger.debug("%s sequence end." % func)
                else:
                    self.CALLS[func](self, args, msg, rmsg)
                self.npipe.send(rmsg.SerializeToString())
                self.logger.debug("%s call finished." % func)
            else:
                self.logger.error("Unknown function called: %s" % func)

    CALLS = {
        "echo": echo_reply,
        "rxsamples": recv_samps,
        "txsamples": xmit_samps,
        "txsine": xmit_sine,
        "measure_power": meas_power,
        #"seq_rxsamples": _do_recv_samps_o,
        "seq_measure":  _do_meas_power,

        "txiq": _do_xmit_samps,
        "rxiq":  master_recv_samps,
        "rxrssi":  _do_rssi,

    }


def parse_args():
    """Parse the command line arguments"""
    parser = argparse.ArgumentParser()
    parser.add_argument("-a", "--args", help="USRP radio arguments", default="", type=str)
    parser.add_argument("-t", "--usetxrx", help="Receive using the TX/RX port", action="store_true")
    parser.add_argument("-c", "--useexternalclock", help="Use external clock", action="store_true")
    parser.add_argument("-xc", "--noexternalclock", help="Do not use external clock", action="store_true")
    parser.add_argument("-s", "--host", help="Orchestrator host to connect to", default=DEF_IP, type=str)
    parser.add_argument("-p", "--port", help="Orchestrator port", default=SERVICE_PORT, type=int)
    parser.add_argument("-d", "--daemon", help="Run as daemon", action="store_true")
    parser.add_argument("-n", "--nickname", help="Nickname for this measurement client (will be communicated to orchestrator).", type=str, default="")
    parser.add_argument("-l", "--logfile", help="Logfile for measurment client.  Mostly for running in daemon mode.", type=str, default=DEF_LOGFILE)
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()
    if args.daemon:
        # Daemonize
        dcxt = daemon.DaemonContext(umask=0o022)
        dcxt.open()
    meascli = MeasurementsClient(args)
    meascli.run()
