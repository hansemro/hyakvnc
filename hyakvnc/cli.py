# SPDX-License-Identifier: MIT
# Copyright (c) 2023 Hansem Ro <hansem7@uw.edu> <hansemro@outlook.com>

# Quick start guide
#
# 1. Build and install hyakvnc package
#      $ python3 -m pip install --upgrade --user pip
#      $ cd hyakvnc
#      $ python3 -m pip install --user .
#
# 2. To start VNC session (on `ece` computing resources) for 5 hours on a node
#    with 16 cores and 32GB of memory, run the following
#      $ hyakvnc create -A ece -p compute-hugemem \
#               -t 5 -c 16 --mem 32G \
#               --container /path/to/container.sif \
#               --xstartup /path/to/xstartup
#      ...
#      =====================
#      Run the following in a new terminal window:
#              ssh -N -f -L 5901:127.0.0.1:5901 hansem7@klone.hyak.uw.edu
#      then connect to VNC session at localhost:5901
#      =====================
#
#    This should print a command to setup port forwarding. Copy it for the
#    following step.
#
# 3. Set up port forward between your computer and HYAK login node.
#    On your machine, in a new terminal window, run the the copied command.
#
#    In this example, run
#      $ ssh -N -f -L 5901:127.0.0.1:5901 hansem7@klone.hyak.uw.edu
#
# 4. Connect to VNC session at instructed address (in example: localhost:5901)
#
# 5. To close VNC session, run the following
#      $ hyakvnc kill-all
#
# 6. Kill port forward process from step 3


# Usage: hyakvnc [-h/--help] [OPTIONS] [COMMAND] [COMMAND_ARGS]
#
# Optional arguments:
#   -h, --help : print help message and exit
#
#   -v, --version : print program version and exit
#
#   -d, --debug : [default: disabled] show debug messages and enable debug
#                 logging at ~/hyakvnc.log
#
#   -J <job_name> : [default: hyakvnc] Override Slurm job name
#
# Commands:
#
#   create : Create VNC session
#
#     Optional arguments for create:
#
#       --timeout : [default: 120] Slurm node allocation and VNC startup timeout
#                   length (in seconds)
#
#       -p <port>, --port <port> : [default: automatically found] override
#                                  User<->LoginNode port
#
#       -G [type:]<ngpus>, --gpus [type:]<ngpus> : [default: '0'] GPU count
#                                                  with optional type specifier
#
#     Required arguments for create:
#
#       -p <part>, --partition <part> : Slurm partition
#
#       -A <account>, --account <account> : Slurm account
#
#       -t <time>, --time <time> : VNC node reservation length in hours
#
#       -c <ncpus>, --cpus <ncpus> : VNC node CPU count
#
#       --mem <size[units]> : [default: 16G] VNC node memory amount
#                             Valid units: K, M, G, T
#
#       --container <sif> : Use specified XFCE+VNC Apptainer container
#
#       --xstartup <xstartup_path> : Use specified xstartup script
#
#   status : print details of active VNC jobs with given job name and exit.
#            Details include the following for each active job:
#              - Job ID
#              - Subnode name
#              - VNC session status
#              - VNC display number
#              - Subnode/VNC port
#              - User/LoginNode port
#              - Time left
#              - SSH port forward command
#
#   kill <job_id> : Kill specified job
#
#   kill-all : Cancel all VNC jobs with given job name and exit
#
#   set-passwd : Prompts for new VNC password and exit
#
#     Required argument for set-passwd:
#
#       --container <sif> : Use specified XFCE+VNC Apptainer container
#
#   repair : Repair all missing/broken LoginNode<->SubNode port
#            forwards, and then exit
#

# Dependencies:
# - Python 3.6 or newer
# - Apptainer 1.0 or newer
# - Slurm
# - netstat utility
# - XFCE container:
#   - xfce4
#   - tigervnc with vncserver

import argparse # for argument handling
import logging # for debug logging
import signal # for signal handling
import glob
import pwd
import os # for path, file/dir checking, hostname
import subprocess # for running shell commands
import re # for regex
from . import __version__
from .hyaknode import *

# Base VNC port cannot be changed due to vncserver not having a stable argument
# interface:
BASE_VNC_PORT = 5900

# List of Klone login node hostnames
LOGIN_NODE_LIST = ["klone-login01", "klone1.hyak.uw.edu", "klone2.hyak.uw.edu"]

# Checked to see if klone is authorized for intracluster access
AUTH_KEYS_FILEPATH = os.path.expanduser("~/.ssh/authorized_keys")

def check_auth_keys():
    """
    Returns True if a public key (~/.ssh/*.pub) exists in
    ~/.ssh/authorized_keys and False otherwise.
    """
    pubkeys = glob.glob(os.path.expanduser("~/.ssh/*.pub"))
    for pubkey in pubkeys:
        cmd = f"cat {pubkey} | grep -f {AUTH_KEYS_FILEPATH} &> /dev/null"
        if subprocess.call(cmd, shell=True) == 0:
            return True
    return False

def create_parser():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='command')

    # general arguments
    parser.add_argument('-d', '--debug',
                    dest='debug',
                    action='store_true',
                    help='Enable debug logging')
    parser.add_argument('-v', '--version',
                    dest='print_version',
                    action='store_true',
                    help='Print program version and exit')
    parser.add_argument('-J',
                    dest='job_name',
                    metavar='<job_name>',
                    help='Slurm job name',
                    default='hyakvnc',
                    type=str)

    # create command
    parser_create = subparsers.add_parser('create',
                    help='Create VNC session')
    parser_create.add_argument('-p', '--partition',
                    dest='partition',
                    metavar='<partition>',
                    help='Slurm partition',
                    required=True,
                    type=str)
    parser_create.add_argument('-A', '--account',
                    dest='account',
                    metavar='<account>',
                    help='Slurm account',
                    required=True,
                    type=str)
    parser_create.add_argument('--timeout',
                    dest='timeout',
                    metavar='<time_in_seconds>',
                    help='[default: 120] Slurm node allocation and VNC startup timeout length (in seconds)',
                    default=120,
                    type=int)
    parser_create.add_argument('--port',
                    dest='u2h_port',
                    metavar='<port_to_hyak>',
                    help='User<->Hyak Port override',
                    type=int)
    parser_create.add_argument('-t', '--time',
                    dest='time',
                    metavar='<time_in_hours>',
                    help='Subnode reservation time (in hours)',
                    required=True,
                    type=int)
    parser_create.add_argument('-c', '--cpus',
                    dest='cpus',
                    metavar='<num_cpus>',
                    help='Subnode cpu count',
                    required=True,
                    type=int)
    parser_create.add_argument('-G', '--gpus',
                    dest='gpus',
                    metavar='[type:]<num_gpus>',
                    help='Subnode gpu count',
                    default='0',
                    type=str)
    parser_create.add_argument('--mem',
                    dest='mem',
                    metavar='<NUM[K|M|G|T]>',
                    help='Subnode memory amount with units',
                    required=True,
                    type=str)
    parser_create.add_argument('--container',
                    dest='sing_container',
                    metavar='<path_to_container.sif>',
                    help='Path to VNC Apptainer/Singularity Container (.sif)',
                    required=True,
                    type=str)
    parser_create.add_argument('--xstartup',
                    dest='xstartup',
                    metavar='<path_to_xstartup>',
                    help='Path to xstartup script',
                    required=True,
                    type=str)

    # status command
    parser_status = subparsers.add_parser('status',
                    help='Print details of all VNC jobs with given job name and exit')

    # kill command
    parser_kill = subparsers.add_parser('kill',
                    help='Kill specified job')
    parser_kill.add_argument('job_id',
                    metavar='<job_id>',
                    help='Kill specified VNC session, cancel its VNC job, and exit',
                    type=int)

    # kill-all command
    parser_kill_all = subparsers.add_parser('kill-all',
                    help='Cancel all VNC jobs with given job name and exit')

    # set-passwd command
    parser_set_passwd = subparsers.add_parser('set-passwd',
                    help='Prompts for new VNC password and exit')
    parser_set_passwd.add_argument('--container',
                    dest='sing_container',
                    metavar='<path_to_container.sif>',
                    help='Path to VNC Apptainer Container (.sif)',
                    required=True,
                    type=str)

    # repair command
    parser_repair = subparsers.add_parser('repair',
                    help='Repair all missing/broken LoginNode<->SubNode port forwards, and then exit')

    return parser

def main():
    parser = create_parser()
    args = parser.parse_args()

    # Debug: setup logging
    if args.debug:
        log_filepath = os.path.expanduser("~/hyakvnc.log")
        print(f"Logging to {log_filepath}...")
        if os.path.exists(log_filepath):
            os.remove(log_filepath)
        logging.basicConfig(filename=log_filepath, level=logging.DEBUG)

        # print passed arguments
        print("Arguments:")
        for item in vars(args):
            msg = f"{item}: {vars(args)[item]}"
            print(f"\t{msg}")
            logging.debug(msg)

    if args.print_version:
        print(f"hyakvnc.py {__version__}")
        exit(0)
    if args.command is None:
        parser.print_help()
        exit(0)

    # check if authorized_keys contains klone to allow intracluster ssh access
    # Reference:
    #  - https://hyak.uw.edu/docs/setup/ssh#intracluster-ssh-keys
    if not os.path.exists(AUTH_KEYS_FILEPATH) or not check_auth_keys():
        if args.debug:
            logging.warning("Warning: Not authorized for intracluster SSH access")
        print("Add SSH public key to ~/.ssh/authorized_keys to continue")
        print(f"\tSee here for more information:")
        print(f"\t\thttps://hyak.uw.edu/docs/setup/ssh#intracluster-ssh-keys")

        # check if ssh key exists
        pr_key_filepath = os.path.expanduser("~/.ssh/id_rsa")
        pub_key_filepath = pr_key_filepath + ".pub"
        if not os.path.exists(pr_key_filepath):
            msg = "Warning: SSH key is missing"
            if args.debug:
                logging.warning(msg)
            print(msg)
            # prompt if user wants to create key
            response = input(f"Create SSH key ({pr_key_filepath})? [y/N] ")
            if re.match("[yY]", response):
                # create key
                print(f"Creating new SSH key ({pr_key_filepath})")
                cmd = f'ssh-keygen -C klone -t rsa -b 4096 -f {pr_key_filepath} -q -N ""'
                if args.debug:
                    print(cmd)
                subprocess.call(cmd, shell=True)
            else:
                msg = "Declined SSH key creation. Quiting program..."
                if args.debug:
                    logging.info(msg)
                print(msg)
                exit(1)

        response = input(f"Add {pub_key_filepath} to ~/.ssh/authorized_keys? [y/N] ")
        if re.match("[yY]", response):
            # add key to authorized_keys
            cmd = f"cat {pub_key_filepath} >> {AUTH_KEYS_FILEPATH}"
            if args.debug:
                print(cmd)
            subprocess.call(cmd, shell=True)
            cmd = f"chmod 600 {AUTH_KEYS_FILEPATH}"
            if args.debug:
                print(cmd)
            subprocess.call(cmd, shell=True)
        else:
            print("Declined SSH key creation. Quiting program...")
            exit(1)
    else:
        if args.debug:
            logging.info("Already authorized for intracluster access.")

    assert os.path.exists(AUTH_KEYS_FILEPATH)

    # delete ~/.ssh/known_hosts in case Hyak maintenance causes node identity
    # mismatch. This can break ssh connection to subnode and cause port
    # forwarding to subnode to fail.
    ssh_known_hosts = os.path.expanduser("~/.ssh/known_hosts")
    if os.path.exists(ssh_known_hosts):
        os.remove(ssh_known_hosts)

    # check if running script on login node
    hostname = os.uname()[1]
    on_subnode = re.match("[ngz]([0-9]{4}).hyak.local", hostname)
    on_loginnode = hostname in LOGIN_NODE_LIST
    if on_subnode or not on_loginnode:
        msg = "Error: Please run on login node."
        print(msg)
        if args.debug:
            logging.error(msg)


    if args.command == 'create':
        assert os.path.exists(args.sing_container)
        assert os.path.exists(args.xstartup)

        # create login node object
        hyak = LoginNode(hostname, args.sing_container, args.xstartup, args.debug)

        # check memory format
        assert(re.match("[0-9]+[KMGT]", args.mem))

        # set VNC password at user's request or if missing
        if not hyak.check_vnc_password():
            if args.debug:
                logging.info("Setting new VNC password...")
            print("Please set new VNC password...")
            hyak.set_vnc_password()

        # reserve node
        subnode = hyak.reserve_node(args.time, args.timeout, args.cpus, args.gpus, args.mem, args.partition, args.account, args.job_name)
        if subnode is None:
            exit(1)

        print(f"...Node {subnode.name} reserved with Job ID: {subnode.job_id}")

        def __irq_handler__(signalNumber, frame):
            """
            Cancel job and exit program.
            """
            if args.debug:
                msg = f"main: Caught signal: {signalNumber}"
                print(msg)
                logging.info(msg)
            print("Cancelling job...")
            hyak.cancel_job(subnode.job_id)
            print("Exiting...")
            exit(1)

        # Cancel job and exit when SIGINT (CTRL+C) or SIGTSTP (CTRL+Z) signals are
        # detected.
        signal.signal(signal.SIGINT, __irq_handler__)
        signal.signal(signal.SIGTSTP, __irq_handler__)

        gpu_count = int(args.gpus.split(':').pop())
        sing_exec_args = ''
        if gpu_count > 0:
            # Use `--nv` apptainer argument to bind CUDA driver and library
            sing_exec_args = '--nv'

        # start vnc
        if not subnode.start_vnc(extra_args=sing_exec_args, timeout=args.timeout):
            hyak.cancel_job(subnode.job_id)
            exit(1)

        # get unused User<->Login port
        if args.u2h_port is not None and hyak.check_port(args.u2h_port):
            hyak.u2h_port = args.u2h_port
        else:
            hyak.u2h_port = hyak.get_port()

        # quit if port is still bad
        if hyak.u2h_port is None:
            msg = "Error: Unable to get port"
            print(msg)
            if args.debug:
                logging.error(msg)
            hyak.cancel_job(subnode.job_id)
            exit(1)

        if args.debug:
            hyak.print_props()

        # create port forward between login and sub nodes
        if not hyak.create_port_forward(hyak.u2h_port, subnode.vnc_port):
            hyak.cancel_job(subnode.job_id)
            exit(1)

        # print command to setup User<->Login port forwarding
        print("=====================")
        print("Run the following in a new terminal window:")
        msg = f"ssh -N -f -L {hyak.u2h_port}:127.0.0.1:{hyak.u2h_port} {os.getlogin()}@klone.hyak.uw.edu"
        print(f"\t{msg}")
        if args.debug:
            logging.debug(msg)
        print(f"then connect to VNC session at localhost:{hyak.u2h_port}")
        print("=====================")
    elif args.command == 'set-passwd':
        assert os.path.exists(args.sing_container)

        # create login node object
        hyak = LoginNode(hostname, args.sing_container, '', args.debug)

        if args.debug:
            logging.info("Setting new VNC password...")
        print("Please set new VNC password...")
        hyak.set_vnc_password()
    elif args.command is not None:
        # create login node object
        hyak = LoginNode(hostname, '', '', args.debug)

        # check for existing subnodes with same job name
        node_set = hyak.find_nodes(args.job_name)

        # get port forwards (and display numbers)
        node_port_map = hyak.get_port_forwards(node_set)

        if args.command == 'repair':
            # repair broken port forwards
            hyak.repair_ln_sn_port_forwards(node_set, node_port_map)
        elif args.command == 'status':
            hyak.print_status(args.job_name, node_set, node_port_map)
        elif args.command == 'kill':
            # kill single VNC job with same job name
            msg = f"Attempting to kill {args.job_id}"
            print(msg)
            if args.debug:
                logging.info(msg)
            if node_set is not None:
                # find target job (with same job name) and quit
                for node in node_set:
                    if re.match(str(node.job_id), str(args.job_id)):
                        if args.debug:
                            logging.info("Found kill target")
                            logging.info(f"\tVNC display number: {node.vnc_display_number}")
                        # kill vnc session
                        port_forward = hyak.get_job_port_forward(node.job_id, node.name, node_port_map)
                        if port_forward:
                            node.kill_vnc(port_forward[0] - BASE_VNC_PORT)
                        # cancel job
                        hyak.cancel_job(args.job_id)
                        exit(0)
            msg = f"{args.job_id} is not claimed or already killed"
            print(f"Error: {msg}")
            if args.debug:
                logging.error(msg)
            exit(1)
        elif args.command == 'kill-all':
            # kill all VNC jobs with same job name
            msg = f"Killing all VNC sessions with job name {args.job_name}..."
            print(msg)
            if args.debug:
                logging.debug(msg)
            if node_set is not None:
                for node in node_set:
                    # kill all vnc sessions
                    node.kill_vnc()
                    # cancel job
                    hyak.cancel_job(node.job_id)
    exit(0)

if __name__ == "__main__":
    main()
