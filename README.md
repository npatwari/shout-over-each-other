# Shout Over Each Other
Neal Patwari, 13 Mar 2026

This repo provides code to:
* Run a shout experiment, but instead of having one node transmit while the other nodes receive, in this shout experiment, we have multiple nodes transmit simultaneously, while the other nodes not transmitting receive.
* Load the data into a python notebook to look at the received data
* Instructions on how to do the above.

### Resource Options Table

Refer to this table as needed to reserve nodes and remember proper gain settings.  

|  Node Type | Nodes | TX/RX Gains |
|---------|----|--------------|
|    Rooftop | `cbrssdr1-bes`, `cbrssdr1-browning`, `cbrssdr1-fm`,  `cbrssdr1-honors`, `cbrssdr1-hospital`, `cbrssdr1-ustar` |  TX: 27, RX:30 |
|  Dense Deployment | `cnode-ebc`, `cnode-guesthouse`, `cnode-mario`, `cnode-moran`, `cnode-ustar` | TX: 80, RX: 70 |


### Instantiate a Shout Experiment
Instantiate a `shout-long-measurement` profile experiment
* Navigate to: https://www.powderwireless.net/
* Go to Experiments: Start Experiment
* Click "Change Profile" and select "shout-long-measurement"
* Click "Next" to get to the "Parameterize" tab.
    - Use a compute node and orchestrator node type of d430
    - Use a Dataset to connect of "None"
    - Expand "CBAND X310 radios." and select the ones to be used. Expand the "Dense Site NUC+B210 radios to allocate." and select the nodes to be used. Of course, you are limited to [what is available](https://www.powderwireless.net/resinfo.php).
    - Expand "Frequency ranges for CBAND operation." You should look at [what is available](https://www.powderwireless.net/resinfo.php). My experiments reserve 2-20 MHz depending on my sample rate settings.
    - Click "Next"
* On the "Finalize" step:
    - Select the "POWDER-Train-26" project, or whatever project that you and your collaborators are in.
* On the "Schedule" step, leave all fields at their defaults (or pick your start and end time) and click "Finish"
* Wait for the experiment to instantiate (turn green), and the startup scripts to finish


### Open Lots of Terminal Windows

We first need to create multiple terminal windows: two logged into the ORCH node, one for each Client node, and one window just on your local laptop.

Run this command into a new terminal window to connect via ssh to the ORCH node.
```
ssh -Y -p 22 -t username@pcWWW.emulab.net 'cd /local/repository/bin && tmux new-session -A -s shout1 &&  exec $SHELL'
```
where `username` is your username, and replace pcWWW with the node name of the orchestrator. This command starts you in the `/local/repository/bin/` directory.

Then get a new tab (a new terminal window) and do the 2nd ORCH terminal:
```
ssh -Y -p 22 -t username@pcWWW.emulab.net 'cd /local/repository/bin && tmux new-session -A -s shout2 &&  exec $SHELL'
```
The only difference is that this tab will be labelled "shout2" instead of "shout1".

Then continue with each node, each in its own terminal window, with 
```
ssh -Y -p 22 -t username@pcWWW.emulab.net 'cd /local/repository/bin && tmux new-session -A -s shout &&  exec $SHELL'

```
where `username` is your username, and replace pcWWW with the node name of the next Client node.


### Check Each SDR's FPGA

If you are using rooftop nodes, you need to check each rooftop node `cbrssdr*` software-defined radio, because they sometimes have an FPGA image different from the one we want for Shout. Do this as follows.

In each of the Client node terminal windows, run `uhd_usrp_probe`. 

If successful, it shows some information about the setup of the SDR.

If instead it shows:
> Error: RuntimeError: Expected FPGA compatibility number 38, but got 39:
> The FPGA image on your device is not compatible with this host code build. 

then do the following steps to flash the FPGA with the correct version of build:
1. Run: `/local/repository/bin/update_x310.sh`, which takes about 5 minutes.
2. When that is complete, Power Cycle the x310: Find the row in the List View of your POWDER experiment page with the name of your node "-x310". Using the Settings icon in that row (all the way to the right), select "Power Cycle" and click "Confirm".
3. After about a minute, run `uhd_find_devices` and `uhd_usrp_probe` to check that the radio is back on and now has the correct FPGA build version.

### Start the Shout Framework


In the ORCH window, run
```
./1.start_orch.sh
```
When it is done, on each compute node (attached to one of the SDRs), run
```
./2.start_client.sh
```
Now Shout is started and ready to "run" the measurement procedure.



### Edit the Experiment Parameters JSON File


You're going to edit the entries of the JSON file `save_iq_w_tx_file.json` parameter structure to have the values corresponding to the experiment you want to run. My version of this file is in this repo. These instructions are for you to edit the file on your local machine and then scp it to all of the nodes. Here's how to set the JSON file, and some notes about how it might change for your experiment:

| Field | Value it should be | Description |
|------|------|------|
| "cmd" | "save_iq_w_tx" | The name of command for the shout code to run. This "save_iq_w_tx" is always the one to use for shout-over-each-other, as it is the command I modified. |
| "sync" | true | True indicates Shout should synchronize transmission and reception | 
| "timeout" | 30 | How many ms before giving up at the TX |
| "nsamps" | 8192 | Number of samples to receive |
| "wotxrepeat" | 0 | How many times the receiver should run when the TX is off (to capture noise samples). You can set this >0, but shout-over-each-other does automatically include the case in which all nodes are receivers. |
| "txrate" | 500e3 | Transmitter sampling rate. Change this to whatever sample rate you want the TX to use. |
|"txfreq"| (Use group setting)| Use the center frequency of the band you reserved. Because it is in MHz, write it with an `e6` at the end. For example, for 3387 MHz as a center frequency, write `3387e6`|
| "rxfreq"|  (Use group setting) | Use the exact same number as "txfreq" |
| "txgain"| `{"fixed": XX.0}` | Here `XX` is your SDR transmitter's gain, either 27 or 80. This is due to the SDR in use: rooftop nodes use a NI X310, DD nodes use the NI B210. See the [hardware documentation](https://docs.powderwireless.net/hardware.html). |
| "txsamps"| `{"file": <TX IQ filename string>}` | The path and filename in quotes. For example, "/local/repository/shout/signal_library/channel-sounding-OFDM-packet-10-03-26.iq". |
| "txwait" | 3 | The transmitter is set to wait a certain duration (in ms, I believe) after the start of the second (when the PPS signal changes)|
| "rxrate" | 500e3 | Sampling rate at the receiver, typically identical to `txrate`. |
| "rxgain" | `{"fixed": YY.0}`| where `YY` is your gain for your SDR receiver, either 30 or 70, depending on what type of SDR you're using.|  
| "rxrepeat" | 4 | How many repetitions will be done of each link measurement |
| "rxwait" | `{"min": 50, "max": 2000, "res": "ms"}` | The time to wait between successive RX operations (when using "rxrepeat") |
| "txclients" | A list of the compute node IDs (in the ID column of List View) | The IDs are the names ending in "-comp" for Groups 1, 2 or 3, or ending in "-dd-b210" for Group 4,5 or 6. For example: `["cbrssdr1-honors-comp", "cbrssdr1-hospital-comp", "cbrssdr1-ustar-comp"]`|
| "rxclients"| The exact same list as for txclients | same as above|

Save the file.

Take a look at the file to double check. In particular check that 
* The rxfreq and txfreq are the same
* The txclients and rxclients are the same, and have strings that end in -comp or -dd-b210

```
cat /local/repository/etc/cmdfiles/save_iq_w_tx_cw.json
```

### Send Files to the ORCH and Client Nodes

Send the .iq file to each client. I run these commands in a terminal on my local laptop. The form is `scp <local file> <username>@<destination-ip>:<file-path>`. We are sending the iq file to all nodes. For my recent experiment, the commands were:
```
scp /Users/neal/git/npatwari/ch-sounding-ofdm-packet/channel-sounding-OFDM-packet-10-03-26.iq npatwari@pc05-fort.emulab.net:/local/repository/shout/signal_library/
scp /Users/neal/git/npatwari/ch-sounding-ofdm-packet/channel-sounding-OFDM-packet-10-03-26.iq npatwari@pc09-fort.emulab.net:/local/repository/shout/signal_library/
scp /Users/neal/git/npatwari/ch-sounding-ofdm-packet/channel-sounding-OFDM-packet-10-03-26.iq npatwari@pc08-fort.emulab.net:/local/repository/shout/signal_library/
scp /Users/neal/git/npatwari/ch-sounding-ofdm-packet/channel-sounding-OFDM-packet-10-03-26.iq npatwari@pc11-fort.emulab.net:/local/repository/shout/signal_library/
```
Change the local file path and name, username, and ip addresses to suit your experiment.


Next, send the .json file to each node. I run these commands in a terminal on my local laptop. The form is `scp <local file> <username>@<destination-ip>:<file-path>`. We are sending the JSON file to all nodes. For my recent experiment, the commands were:
```
scp ./save_iq_w_tx_file.json npatwari@pc05-fort.emulab.net:/local/repository/etc/cmdfiles/ 
scp ./save_iq_w_tx_file.json npatwari@pc09-fort.emulab.net:/local/repository/etc/cmdfiles/ 
scp ./save_iq_w_tx_file.json npatwari@pc11-fort.emulab.net:/local/repository/etc/cmdfiles/ 
scp ./save_iq_w_tx_file.json npatwari@pc08-fort.emulab.net:/local/repository/etc/cmdfiles/ 
```
Change the local file path and name, username, and ip addresses to suit your experiment.

Finally, copy the measiface.py command for shout-over-each-other from your computer to the ORCH. Shout comes with its own measiface.py, but I modified the code in this file to enable simultaneous transmission from all combinations of nodes. So by replacing the existing shout measiface.py with mine, we are bypassing the existing transmit-one-node-at-a-time procedure for the new procedure.


I run these commands in a terminal on my local laptop. The form is `scp <local file> <username>@<destination-ip>:<file-path>`. We are sending the measiface.py ONLY to the ORCH node. For my recent experiment, the command was:
```
scp ./measiface.py npatwari@pc05-fort.emulab.net:/local/repository/shout/
```
Change the local file path and name, username, and ip addresses to suit your experiment.

### Execute the Shout Measurement Command

On your iface-node terminal window, run
```
./3.run_cmd.sh
```
This takes time proportional to (2^N-1), where N is the number of nodes. It will try every possible combination of nodes as transmitters (and remaining nodes as receivers). It does include the case of all nodes as receivers, which gives a baseline noise-only measurement. It does not include the case of all nodes as transmitters, as there would be no nodes to receive.

### Copy Data Back to your Local Laptop

While still on the iface-node terminal window, do a `ls /local/data/` to see your data directory. Copy the directory name. If there is more than one, you probably ran the measurement multiple times, and you can pick whichever one you want.

Back _on a terminal window on your local laptop_, copy the data back to your local laptop. In my case, the command was:
```
scp -r npatwari@pc05-fort.emulab.net:/local/data/Shout_meas_03-10-2026_15-23-32/ data/
```
where, again, change the username, and change pc05-fort to your ORCH name; and change `Shout_meas_03-10-2026_15-23-32` to the directory you saw when running the `ls /local/data/` command, and `data/` to the folder on your local laptop where you're saving your experimental data.

Zip/compress this local directory on your laptop, if you're going to upload it to Google Colab.

### Analyze the Data

In this step, you will load and run a Jupyter notebook, to be posted. 


