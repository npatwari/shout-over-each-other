#!/usr/bin/env python3

# Copyright 2020-2021 University of Utah

import os
import time
import datetime
import argparse
import logging
import json
import multiprocessing as mp
import random


import numpy as np
from itertools import combinations_with_replacement
import math
import h5py

import measurements_pb2 as measpb
from ifaceconnector import InterfaceConnector
from common import *
from rpccalls import *
from sigutils import *
from utils import *

DEF_IP = "127.0.0.1"
DEF_LOGFILE="/var/tmp/measiface.log"
STR_DT = h5py.special_dtype(vlen=str)

class MeasurementsInterface:
    POLLTIME = 10
    DEF_TOFF = 5
    TX_TOFF = 10
    RX_TOFF = 2
    TIMEZONE = 'US/Mountain'

    US_LTE_BANDS = {29: ['SDL', 717, 728], 41: ['TDD', 2570, 2620], #41: ['TDD', 2496, 2690], #41: ['TDD', 2602, 2674]
                    2:['FDD', 1850, 1910, 1930, 1990 ], 4:['FDD', 1710, 1755, 2110, 2155], 
                    5:['FDD', 824, 849, 869, 894], 12:['FDD', 700, 716, 728, 746], 
                    13:['FDD', 777, 787, 746, 756], 14:['FDD', 788, 798, 758, 768], 
                    17:['FDD', 704, 716, 734, 746], 25:['FDD', 1850, 1915, 1930, 1995], 
                    30: ['FDD', 2305, 2315, 2350, 2360], 66:['FDD', 1710, 1780, 2110, 2200],
                    #, 71:['FDD', 663, 698, 617, 652]
                    1000:['DL', 875, 885], # AT&T
                    1001:['DL', 825, 835], # AT&T
                    }

    #BANDS = {'FM': [88, 108], 'ILS+VOR': [108, 117.975], 'Amateur Radio': [144, 148],
             #'': [, ],'': [, ],'': [, ],'': [, ],'': [, ],'': [, ],}

    def __init__(self, args):
        self.clients = {}
        self.pipe = None
        self.conproc = None
        self.datadir = None
        self.dsfile = None
        self.start_time = 0
        self.last_results = []
        self.dfname = args.dfname
        self._setup_logger(args.logfile)
        self._setup_datadir(args.datadir)
        self.connector = InterfaceConnector(args)

    def _setup_logger(self, logfile):
        fmat = logging.Formatter(fmt=LOGFMAT, datefmt=LOGDATEFMAT)
        shandler = logging.StreamHandler()
        shandler.setFormatter(fmat)
        shandler.setLevel(logging.ERROR)
        fhandler = logging.FileHandler(logfile)
        fhandler.setLevel(logging.DEBUG)
        fhandler.setFormatter(fmat)
        self.logger = mp.get_logger()
        self.logger.addHandler(shandler)
        self.logger.addHandler(fhandler)

    def _start_netproc(self):
        (c1, c2) = mp.Pipe()
        self.pipe = c1
        self.netproc = mp.Process(target=self.connector.run, args=(c2,))
        self.netproc.start()
        cmsg = measpb.SessionMsg()
        cmsg.type = measpb.SessionMsg.CALL
        add_attr(cmsg, "funcname", InterfaceConnector.CALL_STATUS)
        while True:
            self.pipe.send(cmsg.SerializeToString())
            rmsg = measpb.SessionMsg()
            rmsg.ParseFromString(self.pipe.recv())
            res = get_attr(rmsg, "result")
            if res == InterfaceConnector.RES_READY:
                break
            time.sleep(1)

    def _stop_netproc(self):
        cmsg = measpb.SessionMsg()
        cmsg.type = measpb.SessionMsg.CALL
        add_attr(cmsg, "funcname", InterfaceConnector.CALL_QUIT)
        self.pipe.send(cmsg.SerializeToString())
        self.netproc.join()
        
    def _setup_datadir(self, ddir):
        self.datadir = ddir
        if not os.path.exists(ddir):
            os.mkdir(ddir)
        
    def _set_start_time(self, toff = DEF_TOFF):
        self.start_time = np.ceil(time.time()) + toff

    def _clear_start_time(self):
        self.start_time = 0

    def _get_connected_clients(self):
        # Get list of clients from the Orchestrator
        cmsg = measpb.SessionMsg()
        cmsg.type = measpb.SessionMsg.CALL
        cmsg.peertype = measpb.SessionMsg.IFACE_CLIENT
        cmsg.uuid = random.getrandbits(31)
        add_attr(cmsg, "funcname", "getclients")
        self.pipe.send(cmsg.SerializeToString())
        rmsg = measpb.SessionMsg()
        rmsg.ParseFromString(self.pipe.recv())
        return rmsg.clients

    def _get_client_list(self, cmd):
        clients = None
        if 'client_list' in cmd:
            clients = list(cmd['client_list'])
        if not clients or clients[0] == "all":
            clients = self._get_connected_clients()
        return clients


    def _get_bus_list(self, cmd):
        clients = self._get_connected_clients()
        buses = [c.split('-')[0].replace('bus', '') for c in clients if 'bus' in c.lower()]
        return buses

    def _rpc_call(self, cmd):
        self.logger.info("Running %s on: %s" % (cmd['cmd'], cmd['client_list']))
        cmsg = RPCCALLS[cmd['cmd']].encode(**cmd)
        cmsg.clients.extend(cmd['client_list'])
        cmsg.peertype = self.connector.ptype
        cmsg.uuid = random.getrandbits(31)
        self.pipe.send(cmsg.SerializeToString())

    def _get_datafile(self):
        if not self.dsfile:
            self.dsfile = h5py.File("%s/%s" % (self.datadir, self.dfname), "a")

        # Add current version of shout
        sv = get_shout_version()
        self.dsfile.attrs.update({"shout_version": sv})

        return self.dsfile


    def _get_rxwait(self, cmd):
        wait = dict()

        if cmd['rxwait']['res'] == 'ms':
            wait['dev'] = 1000.0
        elif cmd['rxwait']['res'] == 's':
            wait['dev'] = 1.0
        else:
            print("Unrecognized res for rxwait = " +cmd['rxwait']['res'])
            exit(1)
        cmd['rxwait_res'] = cmd['rxwait']['res']

        if 'fixed' in cmd['rxwait']:
            wait['random'] = False
            wait['max'] = cmd['rxwait']['fixed']
            wait['min'] = wait['max']

            del cmd['rxwait']
            cmd['rxwait'] = wait['max']
        else:
            wait['random'] = True
            wait['min'] = cmd['rxwait']['min']
            wait['max'] = cmd['rxwait']['max']

            del cmd['rxwait']
            for k in ['random', 'min', 'max']:
                cmd['rxwait_'+k] = wait[k]
        
        return wait


    def _get_gain(self, cmd, t):
        gain = dict()
        if 'fixed' in cmd[t+'gain']:
            gain['start'] = cmd[t+'gain']['fixed']
            gain['end'] = gain['start'] + 1.0
            gain['step'] = 20.0
            gain['total'] = 1

            del cmd[t+'gain']
            cmd[t+'gain'] = gain['start']
        else:
            gain['start'] = cmd[t+'gain']['start']
            gain['end'] = cmd[t+'gain']['end']+cmd[t+'gain']['step']/2.0
            gain['step'] = cmd[t+'gain']['step']
            gain['total'] = int(math.ceil((gain['end']-gain['start'])/gain['step']))

            del cmd[t+'gain']
            for k in ['start', 'end', 'step']:
                cmd[t+'gain_'+k] = gain[k]
        
        return gain

    def _set_tx_samps(self, cmd):
        if 'file' in cmd['txsamps']:
            cmd['txfile'] = cmd['txsamps']['file']
            del cmd['txsamps']
        elif 'signal' in cmd['txsamps'] and cmd['txsamps']['signal'] == 'sine':
            cmd['sine_wampl'] = cmd['txsamps']['wampl']
            cmd['sine_wfreq'] = cmd['txsamps']['wfreq']
            del cmd['txsamps']
        elif 'gold' in cmd['txsamps']:
            del cmd['txsamps']
        else:
            self.logger.error("Unsupported signal type")
            exit(1)
        

    def _get_measgrp(self, cmd, meas_type):
        dfile = self._get_datafile()
        if not meas_type in dfile:
            dfile.create_group(meas_type)
        measgrp = dfile[meas_type].create_group("%d" % int(time.time()))
        measgrp.attrs.update(cmd)
        return measgrp

    def _start_gps_logger(self, clients, duration):
        gpsloggers = []
        buses = [c.replace('bus-', '') for c in clients if 'bus' in c.lower()]
        #print(clients, buses)
        for bus in buses:
            p = mp.Process(target=run_gps_logger, args=(bus, duration, self.datadir, ))
            gpsloggers.append(p)
            p.start()
        return gpsloggers

    def _do_saveiq(self, cmd, clients, grp, repeat, wait, txclients=[]):
        # Prepare command
        rxcmd = RPCCALLS['rxiq'].encode(**cmd)
        rxcmd.peertype = measpb.SessionMsg.IFACE_CLIENT
        
        rxcmd.ClearField("clients")
        rxcmd.clients.extend(clients)
        rxcmd.uuid = random.getrandbits(31)

        self.logger.info("Performing measurement at: %f" % cmd['freq'])
        on_client_repeat = max(cmd['on_client_repeat'], 1) if 'on_client_repeat' in cmd else 1

        nsamps = cmd['nsamps'] #cmd['nsymbols'] if 'gold_id' in cmd and cmd['gold_id'] >= 0 else 
        # Initialize groups and dataset
        for rxclient in clients:
            rxgrp = grp.create_group(rxclient)
            ds = rxgrp.create_dataset('rxtime', (repeat*on_client_repeat,), dtype=STR_DT)
            ds = rxgrp.create_dataset('rxsamples', (repeat*on_client_repeat, nsamps), dtype=np.complex64)
            if 'return_stats' in cmd and cmd['return_stats']:
                ds = rxgrp.create_dataset('ber', (repeat*on_client_repeat, ), dtype=np.float32)
                ds = rxgrp.create_dataset('snr', (repeat*on_client_repeat, ), dtype=np.float32)
                ds = rxgrp.create_dataset('power', (repeat*on_client_repeat, ), dtype=np.float32)
            if 'bus' in rxclient.lower():
                ds = rxgrp.create_dataset('rxloc', (repeat*on_client_repeat, 3), dtype=np.float32)
                ds = rxgrp.create_dataset('rxspeed', (repeat*on_client_repeat, ), dtype=np.float32)
                #ds = rxgrp.create_dataset('rxlocflag', (repeat*on_client_repeat,), dtype=STR_DT)
            if txclients:
                ds = rxgrp.create_dataset('txloc', (repeat*on_client_repeat, len(txclients), 3), dtype=np.float32)
                ds = rxgrp.create_dataset('txspeed', (repeat*on_client_repeat, len(txclients), ), dtype=np.float32)
                #ds = rxgrp.create_dataset('txlocflag', (repeat*on_client_repeat,), dtype=STR_DT)
            
        # Start GPS logging
        gpsduration = int(repeat*(wait['max']/wait['dev']+self.RX_TOFF+cmd['timeout']) + 2.0)
        gpsloggers = self._start_gps_logger(clients, gpsduration)
        
        
        for i in range(repeat):
            rxcmd.start_time = time.time() + self.RX_TOFF
            self.pipe.send(rxcmd.SerializeToString())
            self.cmd_waitres({'client_list': clients,
                              'timeout': cmd['timeout']})

            # Add result
            isPrint = False
            for res in self.last_results:
                rxclient = get_attr(res, 'clientname')
                
                # Get samples
                if res.samples:
                    arr = decode_samples(res)
                    arr_index = 0
                    num_samples = min(arr.shape[-1], grp[rxclient]['rxsamples'][i].shape[-1])
                    for j in range(on_client_repeat):
                        grp[rxclient]['rxsamples'][i*on_client_repeat,:num_samples] = arr[arr_index:arr_index+num_samples]
                        arr_index += num_samples ### May need to be changed.

                # Get measurements
                if res.measurements:
                    arr = np.array(res.measurements, dtype=np.float32)
                    arr_index = 0
                    for j in range(on_client_repeat):
                        measurements_to_dataset(grp[rxclient], i*on_client_repeat+j, arr[arr_index:], rxclient)
                        arr_index += 6 ### May need to be changed.
                    if i < 10 or (i+1)%10 == 0:
                        print('%d --- Power: %0.2f at %0.1f MHz at %s at %s' %(i+1, get_avg_power(grp[rxclient]['rxsamples'][i]), cmd['freq']/1e6, rxclient, grp[rxclient]['rxtime'][i].split(' ')[1]))
                        isPrint = True
                    #mes_count[rxclient] += 1
                
                if 'return_stats' in cmd and cmd['return_stats'] and not cmd['return_iq']:
                    # TODO: check this
                    if res.samples:
                        arr = decode_samples(res)[:3]
                        grp[rxclient]['ber'][i] = arr[0] 
                        grp[rxclient]['snr'][i] = arr[1]
                        grp[rxclient]['power'][i] = arr[2]
                        print('%d --- stats: ber-%0.2f snr-%0.2f power-%0.2f ' %arr[0], arr[1], arr[2])
            if isPrint:
                print('------')
                isPrint = False

            if wait['random']:
                time.sleep(np.random.randint(wait['min'], wait['max'],size=1)[0]/wait['dev'])
            else:
                time.sleep(wait['max']/wait['dev'])

        for p in gpsloggers:
            p.terminate() 

        # Update rxloc and rxspeed for mobile endpoints and, also txloc, txspeed if tx is bus
        for rxclient in clients:
            if 'bus' in rxclient.lower():
                update_rx_loc(grp[rxclient], rxclient, self.datadir)
            update_tx_loc(grp[rxclient], txclients, self.datadir)



    def _do_saverssi(self, cmd, clients, grp, repeat, wait, txclients=[]):
        # Prepare command
        rxcmd = RPCCALLS['rxrssi'].encode(**cmd)
        rxcmd.peertype = measpb.SessionMsg.IFACE_CLIENT
        
        rxcmd.ClearField("clients")
        rxcmd.clients.extend(clients)
        rxcmd.uuid = random.getrandbits(31)

        self.logger.info("Performing measurement at: %f" % cmd['freq'])

        # Initialize groups and dataset
        for rxclient in clients:
            rxgrp = grp.create_group(rxclient)
            ds = rxgrp.create_dataset('rxtime', (repeat,), dtype=STR_DT)
            ds = rxgrp.create_dataset('rssi', (repeat,), dtype=np.float32)

            if 'bus' in rxclient.lower():
                ds = rxgrp.create_dataset('rxloc', (repeat, 3), dtype=np.float32)
                ds = rxgrp.create_dataset('rxspeed', (repeat, ), dtype=np.float32)
                #ds = rxgrp.create_dataset('rxlocflag', (repeat,), dtype=STR_DT)
            if txclients:
                ds = rxgrp.create_dataset('txloc', (repeat, len(txclients), 3), dtype=np.float32)
                ds = rxgrp.create_dataset('txspeed', (repeat, len(txclients), ), dtype=np.float32)
                #ds = rxgrp.create_dataset('txlocflag', (repeat,), dtype=STR_DT)
            
        # Start GPS logging
        gpsduration = int(repeat*(wait['max']/wait['dev']+self.RX_TOFF+cmd['timeout']) + 2.0)
        gpsloggers = self._start_gps_logger(clients, gpsduration)
        
        
        for i in range(repeat):
            rxcmd.start_time = time.time() + self.RX_TOFF
            self.pipe.send(rxcmd.SerializeToString())
            self.cmd_waitres({'client_list': clients,
                              'timeout': cmd['timeout']})

            # Add result
            isPrint = False
            for res in self.last_results:
                rxclient = get_attr(res, 'clientname')

                # Get measurements
                if res.measurements:
                    arr = np.array(res.measurements, dtype=np.float32)
                    rssi_measurements_to_dataset(grp[rxclient], i, arr, rxclient)
                    if i < 10 or (i+1)%10 == 0:
                        print('%d --- Power: %0.2f at %0.1f MHz at %s at %s' %(i+1, grp[rxclient]['rssi'][i], cmd['freq']/1e6, rxclient, grp[rxclient]['rxtime'][i].split(' ')[1]))
                        isPrint = True
                    #mes_count[rxclient] += 1
            if isPrint:
                print('------')
                isPrint = False

            if wait['random']:
                time.sleep(np.random.randint(wait['min'], wait['max'],size=1)[0]/wait['dev'])
            else:
                time.sleep(wait['max']/wait['dev'])

        for p in gpsloggers:
            p.terminate() 

        # Update rxloc and rxspeed for mobile endpoints and, also txloc, txspeed if tx is bus
        for rxclient in clients:
            if 'bus' in rxclient.lower():
                update_rx_loc(grp[rxclient], rxclient, self.datadir)
            update_tx_loc(grp[rxclient], txclients, self.datadir)

        
            
    
    def _do_saveiq_set(self, gain, prntstr, cmd, clients, grp, repeat, wait, txclients=[]):
        for rgain in np.arange(gain['start'], gain['end'], gain['step']): 
            print("******* " + prntstr + "rx gain: " + str(rgain)+" *******" )
            cmd['gain'] = rgain
            cmd['freq'] = cmd['rxfreq']
            cmd['rate'] = cmd['rxrate']
            gaingrp = grp.create_group(str(rgain))
            self._do_saveiq(cmd, clients, gaingrp, repeat, wait, txclients)

    def _do_saverssi_set(self, gain, prntstr, cmd, clients, grp, repeat, wait, txclients=[]):
        for rgain in np.arange(gain['start'], gain['end'], gain['step']): 
            print("******* " + prntstr + "rx gain: " + str(rgain)+" *******" )
            cmd['gain'] = rgain
            cmd['freq'] = cmd['rxfreq']
            cmd['rate'] = cmd['rxrate']
            gaingrp = grp.create_group(str(rgain))
            self._do_saverssi(cmd, clients, gaingrp, repeat, wait, txclients)


    def _do_band_meas(self, cmd, clients, bandgrp, freq_start, freq_end, rxwait):
        for freq in np.arange(freq_start, freq_end, cmd['freq_step']): 
            freqgrp = bandgrp.create_group(str(freq))
            cmd['freq'] = freq
            self._do_saveiq(cmd, clients, freqgrp, cmd['repeat'], rxwait)


    


    def cmd_pause(self, cmd):
        self.logger.info("Pausing for %d seconds" % cmd['duration'])
        time.sleep(cmd['duration'])
        
    def cmd_waitres(self, cmd):
        clients = self._get_client_list(cmd)
        waittime = time.time() + cmd['timeout']
        self.last_results = []
        self.logger.info("Waiting for clients: %s" % clients)
        while time.time() < waittime and len(clients):
            if self.pipe.poll(self.POLLTIME):
                rmsg = measpb.SessionMsg()
                rmsg.ParseFromString(self.pipe.recv())
                clientname = get_attr(rmsg, "clientname")
                self.logger.info("Received result from: %s", clientname)
                for i in range(len(clients)):
                    if clients[i] == clientname:
                        self.last_results.append(rmsg)
                        del clients[i]
                        break
        if time.time() > waittime:
            self.logger.warning("waitres command timed out!")
        if clients:
            self.logger.warning("No result received from: %s" % clients)

    def cmd_plotpsd(self, cmd):
        clients = self._get_client_list(cmd)
        for res in self.last_results:
            clientname = get_attr(res, 'clientname')
            if clientname in clients and res.samples:
                rate = float(get_attr(res, 'rate'))
                vals = decode_samples(res)
                psd = compute_psd(len(vals), vals)
                freqs = np.fft.fftshift(np.fft.fftfreq(len(vals), 1/rate))
                plot_proc(clientname, freqs, psd)

    def cmd_printres(self, cmd):
        clients = self._get_client_list(cmd)
        for res in self.last_results:
            clientname = get_attr(res, 'clientname')
            if clientname in clients:
                print(res)

    def cmd_measpaths(self, cmd):
        txclients = []
        rxclients = []
        if 'client_list' in cmd:
            txclients = self._get_client_list(cmd)
            rxclients = list(txclients)
            del cmd['client_list']
        elif 'txclients' in cmd and 'rxclients' in cmd:
            clidict = {'client_list': cmd['txclients']}
            txclients = self._get_client_list(clidict)
            clidict = {'client_list': cmd['rxclients']}
            rxclients = self._get_client_list(clidict)
            del cmd['txclients'], cmd['rxclients']
        if not 'get_samples' in cmd:
            cmd['get_samples'] = False
        self.logger.info("Running path measurements over clients: TX:%s, RX:%s" % (txclients, rxclients))
        toff = cmd['toff'] if 'toff' in cmd else self.DEF_TOFF
        dfile = self._get_datafile()
        if not 'measure_paths' in dfile:
            dfile.create_group('measure_paths')
        measgrp = dfile['measure_paths'].create_group("%d" % int(time.time()))
        measgrp.attrs.update(cmd)
        cmd['gain'] = cmd['txgain']
        txcmd = RPCCALLS['seq_sine'].encode(**cmd)
        txcmd.peertype = measpb.SessionMsg.IFACE_CLIENT
        cmd['gain'] = cmd['rxgain']
        rxcmd = RPCCALLS['seq_measure'].encode(**cmd)
        rxcmd.peertype = measpb.SessionMsg.IFACE_CLIENT
        del cmd['gain']
        del cmd['cmd']

        for txcli in txclients:
            rxclis = [x for x in rxclients if x != txcli]
            txgrp = measgrp.create_group(txcli)
            stime = rxcmd.start_time = int(time.time()) + 1
            self.logger.info("Running with transmitter: %s" % txcli)
            txcmd.ClearField("clients")
            txcmd.clients.append(txcli)
            rxcmd.ClearField("clients")
            rxcmd.clients.extend(rxclis)
            rxcmd.uuid = random.getrandbits(31)
            self.logger.info("Performing noise measurement.")
            self.pipe.send(rxcmd.SerializeToString())
            self.cmd_waitres({'client_list': rxclis,
                              'timeout': cmd['timeout']})
            for res in self.last_results:
                rxclient = get_attr(res, 'clientname')
                sgrp = txgrp.create_group(rxclient)
                if res.samples:
                    arr = decode_samples(res)
                    ds = sgrp.create_dataset('samples', (2,arr.size),
                                             dtype=arr.dtype)
                    ds[0] = arr
                if res.measurements:
                    arr = np.array(res.measurements, dtype=np.float32)
                    ds = sgrp.create_dataset('avgpower', (2,arr.size), Zdtype=arr.dtype)
                    ds[0] = arr
                    print('Noise power: ', arr)
            rxcmd.start_time = np.ceil(time.time()) + toff
            txcmd.start_time = rxcmd.start_time - self.TX_TOFF
            rxcmd.uuid = random.getrandbits(31)
            txcmd.uuid = random.getrandbits(31)
            self.logger.info("Performing TX power measurement.")
            self.pipe.send(txcmd.SerializeToString())
            self.pipe.send(rxcmd.SerializeToString())
            self.cmd_waitres({'client_list': rxclis + [txcli],
                              'timeout': cmd['timeout']})
            for res in self.last_results:
                rxclient = get_attr(res, 'clientname')
                if res.samples:
                    arr = decode_samples(res)
                    ds = txgrp[rxclient]['samples'][1] = arr
                if res.measurements:
                    arr = np.array(res.measurements, dtype=np.float32)
                    ds = txgrp[rxclient]['avgpower'][1] = arr
                    print('TX power: ', arr)


    def cmd_saveiq(self, cmd):
        # Get RX nodes
        rxclients = self._get_client_list(cmd)
        if 'rxclients' in cmd:
            del cmd['rxclients']
        self.logger.info("Running IQ measurements at clients: %s" % (rxclients))

        # Set general info
        toff = cmd['toff'] if 'toff' in cmd else self.DEF_TOFF
        rxwait = self._get_rxwait(cmd)
        rxgain = self._get_gain(cmd, 'rx')


        # Get measurment group in dataset
        measgrp = self._get_measgrp(cmd, 'saveiq')

        # Start IQ receiving
        self._do_saveiq_set(rxgain, "", cmd, rxclients, measgrp, cmd['rxrepeat'], rxwait)
        self.cmd_waitres({'client_list': rxclients,'timeout': cmd['timeout']})
        print("Done")

    def cmd_saverssi(self, cmd):
        # Get RX nodes
        rxclients = self._get_client_list(cmd)
        if 'rxclients' in cmd:
            del cmd['rxclients']
        self.logger.info("Running IQ measurements at clients: %s" % (rxclients))

        # Set general info
        toff = cmd['toff'] if 'toff' in cmd else self.DEF_TOFF
        rxwait = self._get_rxwait(cmd)
        rxgain = self._get_gain(cmd, 'rx')


        # Get measurment group in dataset
        measgrp = self._get_measgrp(cmd, 'saverssi')

        # Start IQ receiving
        self._do_saverssi_set(rxgain, "", cmd, rxclients, measgrp, cmd['rxrepeat'], rxwait)
        self.cmd_waitres({'client_list': rxclients,'timeout': cmd['timeout']})
        print("Done")


    def cmd_saveiqwtx(self, cmd): # For sequential transmitters
        #self.logger.setLevel(logging.ERROR)
        
        # Get RX and TX nodes
        clidict = {'client_list': cmd['txclients']}
        txclients = self._get_client_list(clidict)
        clidict = {'client_list': cmd['rxclients']}
        rxclients = self._get_client_list(clidict)
        del cmd['txclients'], cmd['rxclients']
        self.logger.info("Starting IQ rx over clients: TX:%s, RX:%s" % (txclients, rxclients))
        
        # Set general info
        toff = cmd['toff'] if 'toff' in cmd else self.TX_TOFF
        rxwait = self._get_rxwait(cmd)
        txgain = self._get_gain(cmd, 'tx')
        rxgain = self._get_gain(cmd, 'rx')
        self._set_tx_samps(cmd)
        
        
        # Get measurment group in dataset
        measgrp = self._get_measgrp(cmd, "saveiq_w_tx" )

        # Compute total measurements
        wo_tx_repeat = cmd['wotxrepeat'] if 'wotxrepeat' in cmd else 10
        totalmeas = txgain['total']*rxgain['total']*cmd['rxrepeat']
        cmd['duration'] = int(rxgain['total']*cmd['rxrepeat']*(rxwait['max']/rxwait['dev']+self.RX_TOFF)+cmd['timeout']) # duration of one tx
        #gpsduration = int(wo_tx_repeat*(rxwait['max']/rxwait['dev']+self.RX_TOFF) + 10 + txgain['total']*(cmd['duration']+cmd['txwait']+toff))
        print("Total measurmemets with TX = %d, one TX duration = %d" %(totalmeas, cmd['duration']))


        # First, measure without TX
        grp = measgrp.create_group("wo_tx")
        self._do_saveiq_set(rxgain, "", cmd, rxclients, grp,  wo_tx_repeat, rxwait)
            
        # Now, measure with TX 
        for txcli in txclients:
            print("Transmitting from: %s" %txcli)
            
            if 'bus' in txcli:
                tx = [txcli]
                gpsloggers = self._start_gps_logger(tx, cmd['duration'])
            else:
                tx = []
                gpsloggers = []

            # Create group
            txgrp = measgrp.create_group(txcli)
            
            for tgain in np.arange(txgain['start'], txgain['end'], txgain['step']):
                # Prepare and send command to TX
                cmd['gain'] = tgain
                cmd['freq'] = cmd['txfreq']
                cmd['rate'] = cmd['txrate']
                txcmd = RPCCALLS['txiq'].encode(**cmd)
                txcmd.peertype = measpb.SessionMsg.IFACE_CLIENT
                txcmd.ClearField("clients")
                txcmd.clients.append(txcli)
                txcmd.start_time = time.time() + toff
                txcmd.uuid = random.getrandbits(31)
                self.pipe.send(txcmd.SerializeToString())

                time.sleep(cmd['txwait'])
                tx_endtime = txcmd.start_time + cmd['duration']

                # Create group
                txgaingrp = txgrp.create_group(str(tgain))

                # Start IQ receiving
                self._do_saveiq_set(rxgain, "tx gain: "+str(tgain) + ", ", cmd, [x for x in rxclients if x != txcli], txgaingrp, cmd['rxrepeat'], rxwait)

                #time.sleep(cmd['txwait'])

                sltime = tx_endtime - time.time() + 5
                if sltime > 0:
                    time.sleep(sltime)
         
            self.cmd_waitres({'client_list': [txcli],'timeout': cmd['timeout']})
            print("Done")

            for p in gpsloggers:
                p.terminate() 


    def cmd_saveiqwsimultx(self, cmd): # For simultaneous transmitters
        #self.logger.setLevel(logging.ERROR)
        
        # Get RX and TX nodes
        clidict = {'client_list': cmd['txclients']}
        txclients = self._get_client_list(clidict)
        clidict = {'client_list': cmd['rxclients']}
        rxclients = self._get_client_list(clidict)
        del cmd['txclients'], cmd['rxclients']
        self.logger.info("Starting IQ rx over clients: TX:%s, RX:%s" % (txclients, rxclients))
        
        # Set general info
        toff = cmd['toff'] if 'toff' in cmd else self.TX_TOFF
        rxwait = self._get_rxwait(cmd)
        rxgain = self._get_gain(cmd, 'rx')


        # Create tx setups
        setups = [None]*cmd['txsetupcount']
        np.random.seed()

        
        gainslist = np.arange(cmd['txgain']['start'], cmd['txgain']['end'], cmd['txgain']['step'])
        #print(cmd['txgain'], np.arange(cmd['txgain']['start'], cmd['txgain']['end'], cmd['txgain']['step']))
        if cmd['txcount'] > 1:
            txgains = []
            for gains in combinations_with_replacement(gainslist, cmd['txcount']):
                if max(np.diff(gains)) <= cmd['txgaindiff']:
                    txgains.append(gains)
        else:
            txgains = [[g] for g in gainslist]

        txgain = self._get_gain(cmd, 'tx')
        bwmin = 2e6
        bwmax = (cmd['rxrate'] - (cmd['txcount']+1)*cmd['txguard']) - bwmin
        if 'txbwlist' not in cmd:
            if cmd['txcount'] == 1:
                txbwlist = np.arange(1e6, 30e6, 2e6)
            else:
                txbwlist = np.arange(bwmin, bwmax, 1e6)
        cfstep = 1e6

        cfmax_step = cmd['txguard'] + bwmax

        for i in range(cmd['txsetupcount']):
            setups[i] = dict()
            setups[i]['txs'] = np.random.choice(txclients, cmd['txcount'], replace=False)
            setups[i]['signals'] = np.random.choice(cmd['txsignals'], cmd['txcount'])
            setups[i]['gains'] = txgains[np.random.choice(range(len(txgains)), 1)[0]]
            #setups[i]['gains'] = txgains[i%3]#setups[i]['signals'] = txgains[i%16]

            setups[i]['bws'] = []
            setups[i]['freqs'] = []
            
            cfmin = round_center_freq(cmd['rxfreq'] - cmd['rxrate']/2 + cmd['txguard'], 1e6)
            for j in range(cmd['txcount']):
                bw = np.random.choice([b for b in txbwlist if b <= bwmax], 1)[0]
                #bw = np.random.choice(txbwlist, 1)[0] #TO-DO: extend for multiple signals
                #bw = txbwlist[i//3]
                setups[i]['bws'].append(bw)

                '''if 'OFDM' not in setups[i]['signals'][j]:
                    comp = setups[i]['signals'][j].split('/')[-1].split('_')
                    rof = float(comp[1])
                    oss = float(comp[2])
                    bw = (bw/oss)*(1+rof)'''

                
                if j < cmd['txcount'] - 1:
                    cf = round_center_freq(cfmin+bw/2, 1e6)
                else:
                    cfmax = round_center_freq(cmd['rxfreq'] + cmd['rxrate']/2 - cmd['txguard'], 1e6)
                    cf_range = np.arange(cfmin+bw/2, cfmax-bw/2, cfstep)
                    if len(cf_range) == 0:
                        cf = cfmin+bw/2
                    else:
                        cf = np.random.choice(cf_range, 1)[0]
                cf = round_center_freq(cf, 1e6)
                setups[i]['freqs'].append(cf)

                cfmin = round_center_freq(cf + bw/2 + cmd['txguard'], 1e6)
                bwmax = (cmd['rxrate'] - (cmd['txcount']+1)*cmd['txguard']) - bw

        #print(setups)
        #return
        del cmd['txsignals']
        np.save(self.datadir+'/txsetups', setups)
        
        # Get measurment group in dataset
        measgrp = self._get_measgrp(cmd, "save_iq_w_simul_tx")
        

        # First, measure without TX
        wo_tx_repeat = cmd['wotxrepeat'] if 'wotxrepeat' in cmd else 10
        grp = measgrp.create_group("wo_tx")
        self._do_saveiq_set(rxgain, "", cmd, rxclients, grp,  wo_tx_repeat, rxwait)

        # Compute total measurements
        totalmeas = rxgain['total']*cmd['rxrepeat']
        # TODO: check duration count
        cmd['duration'] = cmd['txwait'] + int(rxgain['total']*cmd['rxrepeat']*(rxwait['max']/rxwait['dev']+cmd['timeout']+self.RX_TOFF)) # duration of one tx
        #gpsduration = int(wo_tx_repeat*(rxwait['max']/rxwait['dev']+self.RX_TOFF) + 10 + txgain['total']*(cmd['duration']+cmd['txwait']+toff))
        print("Total measurmemets with TX = %d, one TX duation = %d" %(totalmeas, cmd['duration']))

        # TODO: Start GPS loggers
        gpsloggers = []

        # Go through tx setups
        for s, setup in enumerate(setups):
            print(setup)
            
            # Create group
            txgrp = measgrp.create_group(str(s))

            # Transmit from all TXs
            txstart = time.time()
            for t in range(cmd['txcount']):
                txcli = setup['txs'][t]
                print("Transmitting from: %s" %txcli)
                
                # Prepare and send command to TX
                cmd['gain'] = setup['gains'][t]
                cmd['freq'] = setup['freqs'][t]
                cmd['rate'] = setup['bws'][t]
                cmd['txfile'] = setup['signals'][t]
                txcmd = RPCCALLS['txiq'].encode(**cmd)
                txcmd.peertype = measpb.SessionMsg.IFACE_CLIENT
                txcmd.ClearField("clients")
                txcmd.clients.append(txcli)
                txcmd.start_time = time.time() + toff
                txcmd.uuid = random.getrandbits(31)
                self.pipe.send(txcmd.SerializeToString())

            time.sleep(cmd['txwait'])

            # Start IQ receiving
            self._do_saveiq_set(rxgain, "", cmd, [x for x in rxclients if x != txcli], txgrp, cmd['rxrepeat'], rxwait)

            sltime = txstart + cmd['duration'] - time.time() + 60
            if sltime > 0:
                time.sleep(sltime)

        # End gpsloggers
        for p in gpsloggers:
            p.terminate() 


    def cmd_measlte(self, cmd):
        # Set RX nodes
        if 'rxclients' in cmd:
            clidict = {'client_list': cmd['rxclients']}
        else:
            cldicr = cmd
        clients = self._get_client_list(clidict)
        self.logger.info("Running power measurements of LTE bands at clients: %s" % (clients))


        # Set dataset
        dfile = self._get_datafile()
        meas_type = 'lte_meas'
        measgrp = dfile.create_group(meas_type)

        # Set general info
        rxwait = self._get_rxwait(cmd)
        
        if len(cmd['bands']) != 0:
            lte_bands = list(cmd['bands'])
        else:
            lte_bands = list(self.US_LTE_BANDS.keys())

        for band in lte_bands:
            if self.US_LTE_BANDS[band][0] != "FDD":
                freq_start = self.US_LTE_BANDS[band][1]*1e6 + cmd['rate']//2
                freq_end = self.US_LTE_BANDS[band][2]*1e6  + cmd['rate']//2
                bandgrp = measgrp.create_group(self.US_LTE_BANDS[band][0] + "_" + str(band) + "_" + str(self.US_LTE_BANDS[band][1]) + "_" + str(self.US_LTE_BANDS[band][2]))
                self._do_band_meas(cmd, clients, bandgrp, freq_start, freq_end, rxwait)
            else:
                # UL
                freq_start = self.US_LTE_BANDS[band][1]*1e6  + cmd['rate']//2
                freq_end = self.US_LTE_BANDS[band][2]*1e6  + cmd['rate']//2
                bandgrp = measgrp.create_group("UL" + "_" + str(band) + "_" + str(self.US_LTE_BANDS[band][1]) + "_" + str(self.US_LTE_BANDS[band][2]))
                self._do_band_meas(cmd, clients, bandgrp, freq_start, freq_end, rxwait)

                # DL
                freq_start = self.US_LTE_BANDS[band][3]*1e6  + cmd['rate']//2
                freq_end = self.US_LTE_BANDS[band][4]*1e6  + cmd['rate']//2
                bandgrp = measgrp.create_group("DL" + "_" + str(band) + "_" + str(self.US_LTE_BANDS[band][3]) + "_" + str(self.US_LTE_BANDS[band][4]))
                self._do_band_meas(cmd, clients, bandgrp, freq_start, freq_end, rxwait)

        self.cmd_waitres({'client_list': clients,'timeout': cmd['timeout']})
        print("\tFinally done")


    


    def run(self, cmdfile):
        self._start_netproc()

        # Read in and execute commands
        with open(cmdfile) as cfile:
            commands = json.load(cfile)
            for cmd in commands:
                cmd['timezone'] = self.TIMEZONE
                if 'sync' in cmd and cmd['sync'] == True:
                    if 'toff' in cmd:
                        self._set_start_time(cmd['toff'])
                    elif not self.start_time:
                        self._set_start_time()
                    cmd['start_time'] = self.start_time
                else:
                    self._clear_start_time()

                if cmd["cmd"] in self.CMD_DISPATCH:
                    self.CMD_DISPATCH[cmd["cmd"]](self, cmd)
                else:
                    self._rpc_call(cmd)

        self.logger.info("Done with commands...")
        self._stop_netproc()
        self.dsfile.close()

    CMD_DISPATCH = {
        "pause":         cmd_pause,
        "wait_results":  cmd_waitres,
        "plot_psd":      cmd_plotpsd,
        "print_results": cmd_printres,
        "measure_paths": cmd_measpaths,
        "save_iq_w_tx": cmd_saveiqwtx,
        "save_iq_w_simul_tx": cmd_saveiqwsimultx,
        "save_iq": cmd_saveiq,
        "save_rssi": cmd_saverssi,
        "measure_lte": cmd_measlte,
    }


def parse_args():
    """Parse the command line arguments"""
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--cmdfile", help="JSON command file to execute", type=str, required=True)
    parser.add_argument("-l", "--logfile", type=str, default=DEF_LOGFILE)
    parser.add_argument("-o", "--datadir", help="Directory to save data into. Default: %s" % DATA_OUTDIR, type=str, default=DATA_OUTDIR)
    parser.add_argument("-f", "--dfname", help="HDF5 results file (inside 'datadir'). Default: %s" % DATA_FILE_NAME, type=str, default=DATA_FILE_NAME)
    parser.add_argument("-n", "--nickname", help="Nickname for this interface client.  Don't use...", type=str, default="")
    parser.add_argument("-s", "--host", help="Orchestrator hostname or IP address", default=DEF_IP, type=str)
    parser.add_argument("-p", "--port", help="Orchestrator network port. Default: %s" % SERVICE_PORT, default=SERVICE_PORT, type=int)
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()
    meas = MeasurementsInterface(args)
    meas.run(args.cmdfile)
