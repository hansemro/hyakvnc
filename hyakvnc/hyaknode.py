# SPDX-License-Identifier: MIT
# Copyright (c) 2023 Hansem Ro <hansem7@uw.edu> <hansemro@outlook.com>

import logging # for debug logging
import time # for sleep
import signal # for signal handling
import glob
import os # for path, file/dir checking, hostname
import subprocess # for running shell commands
import re # for regex

# Base VNC port cannot be changed due to vncserver not having a stable argument
# interface:
BASE_VNC_PORT = 5900

# Full path to Apptainer binary (formerly Singularity)
APPTAINER_BIN = "/sw/apptainer/default/bin/apptainer"

# Apptainer bindpaths can be overwritten if $APPTAINER_BINDPATH is defined.
# Bindpaths are used to mount storage paths to containerized environment.
APPTAINER_BINDPATH = os.getenv("APPTAINER_BINDPATH")
if APPTAINER_BINDPATH is None:
    APPTAINER_BINDPATH = os.getenv("SINGULARITY_BINDPATH")
if APPTAINER_BINDPATH is None:
    APPTAINER_BINDPATH = "/tmp,$HOME,$PWD,/gscratch,/opt,/:/hyak_root,/sw,/mmfs1"

class Node:
    """
    The Node class has the following initial data: bool: debug, string: name.

    debug: Print and log debug messages if True.
    name: Shortened hostname of node.
    """

    def __init__(self, name, sing_container, xstartup, debug=False):
        self.debug = debug
        self.name = name
        self.sing_container = os.path.abspath(sing_container)
        self.xstartup = os.path.abspath(xstartup)

    def get_sing_exec(self, args=''):
        """
        Added before command to execute inside an apptainer (singularity) container.

        Arg:
          args: Optional arguments passed to `apptainer exec`

        Return apptainer exec string
        """
        return f"{APPTAINER_BIN} exec {args} -B {APPTAINER_BINDPATH} {self.sing_container}"

class SubNode(Node):
    """
    The SubNode class specifies a node requested via Slurm (also known as work
    or interactive node). SubNode class is initialized with the following:
    bool: debug, string: name, string: hostname, int: job_id.

    SubNode class with active VNC session may contain vnc_display_number and
    vnc_port.

    debug: Print and log debug messages if True.
    name: Shortened subnode hostname (e.g. n3000) described inside `/etc/hosts`.
    hostname: Full subnode hostname (e.g. n3000.hyak.local).
    job_id: Slurm Job ID that allocated the node.
    vnc_display_number: X display number used for VNC session.
    vnc_port: vnc_display_number + BASE_VNC_PORT.
    """

    def __init__(self, name, job_id, sing_container, xstartup, debug=False):
        super().__init__(name, sing_container, xstartup, debug)
        self.hostname = f"{name}.hyak.local"
        self.job_id = job_id
        self.vnc_display_number = None
        self.vnc_port = None

    def print_props(self):
        """
        Print properties of SubNode object.
        """
        print("SubNode properties:")
        props = vars(self)
        for item in props:
            msg = f"{item} : {props[item]}"
            print(f"\t{msg}")
            if self.debug:
                logging.debug(msg)

    def run_command(self, command:str, timeout=None):
        """
        Run command (with arguments) on subnode

        Args:
          command:str : command and its arguments to run on subnode
          timeout : [Default: None] timeout length in seconds

        Returns ssh subprocess with stderr->stdout and stdout->PIPE
        """
        assert self.name is not None
        cmd = ["ssh", self.hostname, command]
        if timeout is not None:
            cmd.insert(0, "timeout")
            cmd.insert(1, str(timeout))
        if self.debug:
            msg = f"Running on {self.name}: {cmd}"
            print(msg)
            logging.info(msg)
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def list_pids(self):
        """
        Returns list of PIDs of current job_id using
        `scontrol listpids <job_id>`.
        """
        ret = list()
        cmd = f"scontrol listpids {self.job_id}"
        proc = self.run_command(cmd)
        while proc.poll() is None:
            line = str(proc.stdout.readline(), "utf-8").strip()
            if self.debug:
                msg = f"list_pids: {line}"
                logging.debug(msg)
            if "PID" in line:
                pass
            elif re.match("[0-9]+", line):
                pid = int(line.split(' ', 1)[0])
                ret.append(pid)
        return ret

    def check_pid(self, pid:int):
        """
        Returns True if given pid is active in job_id and False otherwise.
        """
        return pid in self.list_pids()

    def get_vnc_pid(self, hostname, display_number):
        """
        Returns pid from file <hostname>:<display_number>.pid or None if file
        does not exist.
        """
        if hostname is None:
            hostname = self.hostname
        if display_number is None:
            display_number = self.vnc_display_number
        assert(hostname is not None)
        if display_number is not None:
            filepaths = glob.glob(os.path.expanduser(f"~/.vnc/{hostname}*:{display_number}.pid"))
            for path in filepaths:
                try:
                    f = open(path, "r")
                except:
                    pass
                return int(f.readline())
        return None

    def check_vnc(self):
        """
        Returns True if VNC session is active and False otherwise.
        """
        assert(self.name is not None)
        assert(self.job_id is not None)
        pid = self.get_vnc_pid(self.hostname, self.vnc_display_number)
        if pid is None:
            pid = self.get_vnc_pid(self.name, self.vnc_display_number)
            if pid is None:
                return False
        if self.debug:
            logging.debug(f"check_vnc: Checking VNC PID {pid}")
        return self.check_pid(pid)

    def start_vnc(self, display_number=None, extra_args='', timeout=20):
        """
        Starts VNC session

        Args:
          display_number: Attempt to acquire specified display number if set.
                          If None, then let vncserver determine display number.
          extra_args: Optional arguments passed to `apptainer exec`
          timeout: timeout length in seconds

        Returns True if VNC session was started successfully and False otherwise
        """
        target = ""
        if display_number is not None:
            target = f":{display_number}"
        vnc_cmd = f"{self.get_sing_exec(extra_args)} vncserver {target} -xstartup {self.xstartup} &"
        if not self.debug:
            print("Starting VNC server...", end="", flush=True)
        proc = self.run_command(vnc_cmd, timeout=timeout)

        # get display number and port number
        while proc.poll() is None:
            line = str(proc.stdout.readline(), 'utf-8').strip()

            if line is not None:
                if self.debug:
                    logging.debug(f"start_vnc: {line}")
                if "desktop" in line:
                    # match against the following pattern:
                    #New 'n3000.hyak.local:1 (hansem7)' desktop at :1 on machine n3000.hyak.local
                    #New 'n3000.hyak.local:6 (hansem7)' desktop is n3000.hyak.local:6
                    pattern = re.compile("""
                            (New\s)
                            (\'([^:]+:(?P<display_number>[0-9]+))\s([^\s]+)\s)
                            """, re.VERBOSE)
                    match = re.match(pattern, line)
                    assert match is not None
                    self.vnc_display_number = int(match.group("display_number"))
                    self.vnc_port = self.vnc_display_number + BASE_VNC_PORT
                    if self.debug:
                        logging.debug(f"Obtained display number: {self.vnc_display_number}")
                        logging.debug(f"Obtained VNC port: {self.vnc_port}")
                    else:
                        print('\x1b[1;32m' + "Success" + '\x1b[0m')
                    return True
        if self.debug:
            logging.error("Failed to start vnc session (Timeout/?)")
        else:
            print('\x1b[1;31m' + "Timed out" + '\x1b[0m')
        return False

    def list_vnc(self):
        """
        Returns a list of active and stale vnc sessions on subnode.
        """
        active = list()
        stale = list()
        cmd = f"{self.get_sing_exec()} vncserver -list"
        #TigerVNC server sessions:
        #
        #X DISPLAY #	PROCESS ID
        #:1		7280 (stale)
        #:12		29 (stale)
        #:2		83704 (stale)
        #:20		30
        #:3		84266 (stale)
        #:4		90576 (stale)
        pattern = re.compile(r":(?P<display_number>\d+)\s+\d+(?P<stale>\s\(stale\))?")
        proc = self.run_command(cmd)
        while proc.poll() is None:
            line = str(proc.stdout.readline(), "utf-8").strip()
            match = re.search(pattern, line)
            if match is not None:
                display_number = match.group("display_number")
                if match.group("stale") is not None:
                    stale.append(display_number)
                else:
                    active.append(display_number)
        return (active,stale)

    def __remove_files__(self, filepaths:list):
        """
        Removes files on subnode and returns True on success and False otherwise.

        Arg:
          filepaths: list of file paths to remove. Each entry must be a file
                     and not a directory.
        """
        cmd = f"rm -f"
        for path in filepaths:
            cmd = f"{cmd} {path}"
        cmd = f"{cmd} &> /dev/null"
        if self.debug:
            logging.debug(f"Calling ssh {self.hostname} {cmd}")
        return subprocess.call(['ssh', self.hostname, cmd]) == 0

    def __listdir__(self, dirpath):
        """
        Returns a list of contents inside directory.
        """
        ret = list()
        cmd = f"test -d {dirpath} && ls -al {dirpath} | tail -n+4"
        pattern = re.compile("""
            ([^\s]+\s+){8}
            (?P<name>.*)
            """, re.VERBOSE)
        proc = self.run_command(cmd)
        while proc.poll() is None:
            line = str(proc.stdout.readline(), "utf-8").strip()
            match = re.match(pattern, line)
            if match is not None:
                name = match.group("name")
                ret.append(name)
        return ret

    def kill_vnc(self, display_number=None):
        """
        Kill specified VNC session with given display number or all VNC sessions.
        """
        if display_number is None:
            active,stale = self.list_vnc()
            for entry in active:
                if self.debug:
                    logging.debug(f"kill_vnc: active entry: {entry}")
                self.kill_vnc(entry)
            for entry in stale:
                if self.debug:
                    logging.debug(f"kill_vnc: stale entry: {entry}")
                self.kill_vnc(entry)
            # Remove all remaining pid files
            pid_list = glob.glob(os.path.expanduser("~/.vnc/*.pid"))
            for pid_file in pid_list:
                try:
                    os.remove(pid_file)
                except:
                    pass
            # Remove all owned socket files on subnode
            # Note: subnode maintains its own /tmp/ directory
            x11_unix = "/tmp/.X11-unix"
            ice_unix = "/tmp/.ICE-unix"
            file_targets = list()
            for entry in self.__listdir__(x11_unix):
                file_targets.append(f"{x11_unix}/{entry}")
            for entry in self.__listdir__(ice_unix):
                file_targets.append(f"{x11_unix}/{entry}")
            self.__remove_files__(file_targets)
        else:
            assert display_number is not None
            target = f":{display_number}"
            if self.debug:
                print(f"Attempting to kill VNC session {target}")
                logging.debug(f"Attempting to kill VNC session {target}")
            cmd = f"{self.get_sing_exec()} vncserver -kill {target}"
            proc = self.run_command(cmd)
            killed = False
            while proc.poll() is None:
                line = str(proc.stdout.readline(), "utf-8").strip()
                # Failed attempt:
                #Can't kill '29': Operation not permitted
                #Killing Xtigervnc process ID 29...
                # On successful attempt:
                #Killing Xtigervnc process ID 29... success!
                if self.debug:
                    logging.debug(f"kill_vnc: {line}")
                if "success" in line:
                    killed = True
            if self.debug:
                logging.debug(f"kill_vnc: killed? {killed}")
            # Remove target's pid file if present
            try:
                os.remove(os.path.expanduser(f"~/.vnc/{self.hostname}{target}.pid"))
            except:
                pass
            try:
                os.remove(os.path.expanduser(f"~/.vnc/{self.name}{target}.pid"))
            except:
                pass
            # Remove associated /tmp/.X11-unix/<display_number> socket
            socket_file = f"/tmp/.X11-unix/{display_number}"
            self.__remove_files__([socket_file])

class LoginNode(Node):
    """
    The LoginNode class specifies Hyak login node for its Slurm and SSH
    capabilities.
    """

    def __init__(self, name, sing_container, xstartup, debug=False):
        assert os.path.exists(APPTAINER_BIN)
        super().__init__(name, sing_container, xstartup, debug)
        self.subnode = None

    def find_nodes(self, job_name="vnc"):
        """
        Returns a set of subnodes with given job name and returns None otherwise
        """
        ret = set()
        command = f"squeue | grep {os.getlogin()} | grep {job_name}"
        proc = self.run_command(command)
        while True:
            line = str(proc.stdout.readline(), 'utf-8')
            if self.debug:
                logging.debug(f"find_nodes: {line}")
            if not line:
                if not ret:
                    return None
                return ret
            if os.getlogin() in line:
                # match against pattern:
                #            864877 compute-h      vnc  hansem7  R       4:05      1 n3000
                # or the following if a node is in the process of being acquired
                #            870400 compute-h      vnc  hansem7 PD       0:00      1 (Resources)
                # or the following if a node failed to be acquired and needs to be killed
                #            984669 compute-h      vnc  hansem7 PD       0:00      1 (QOSGrpCpuLimit)
                pattern = re.compile("""
                        (\s+)
                        (?P<job_id>[0-9]+)
                        (\s+[^ ]+){6}
                        (\s+)
                        (?P<subnode_name>[^\s]+)
                        """, re.VERBOSE)
                match = pattern.match(line)
                assert match is not None
                name = match.group("subnode_name")
                job_id = match.group("job_id")
                if "Resources" in name:
                    # Quit if another node is being allocated (from another process?)
                    proc.kill()
                    msg = f"Warning: Already allocating node with job {job_id}"
                    print(msg)
                    if self.debug:
                        logging.info(f"name: {name}")
                        logging.info(f"job_id: {job_id}")
                        logging.warning(msg)
                elif "QOS" in name:
                    proc.kill()
                    msg = f"Warning: job {job_id} needs to be killed"
                    print(msg)
                    print(f"Please run this script again with '--kill {job_id}' argument")
                    if self.debug:
                        logging.info(f"name: {name}")
                        logging.info(f"job_id: {job_id}")
                        logging.warning(msg)
                elif self.debug:
                    msg = f"Found active subnode {name} with job ID {job_id}"
                    logging.debug(msg)
                tmp = SubNode(name, job_id, '', '', self.debug)
                ret.add(tmp)
        return None

    def check_vnc_password(self):
        """
        Returns True if vnc password is set and False otherwise
        """
        return os.path.exists(os.path.expanduser("~/.vnc/passwd"))

    def set_vnc_password(self):
        """
        Set VNC password
        """
        cmd = f"{self.get_sing_exec()} vncpasswd"
        self.call_command(cmd)

    def call_command(self, command:str):
        """
        Call command (with arguments) on login node (to allow user interaction).

        Args:
          command:str : command and its arguments to run on subnode

        Returns command exit status
        """
        if self.debug:
            msg = f"Calling on {self.name}: {command}"
            print(msg)
            logging.debug(msg)
        return subprocess.call(command, shell=True)

    def run_command(self, command):
        """
        Run command (with arguments) on login node.
        Commands can be in either str or list format.

        Example:
          cmd_str = "echo hi"
          cmd_list = ["echo", "hi"]

        Args:
          command : command and its arguments to run on login node

        Returns subprocess with stderr->stdout and stdout->PIPE
        """
        assert command is not None
        if self.debug:
            msg = f"Running on {self.name}: {command}"
            print(msg)
            logging.debug(msg)
        if isinstance(command, list):
            return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        elif isinstance(command, str):
            return subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def reserve_node(self, res_time=3, timeout=10, cpus=8, gpus="0", mem="16G", partition="compute-hugemem", account="ece", job_name="vnc"):
        """
        Reserves a node and waits until the node has been acquired.

        Args:
          res_time: Number of hours to reserve sub node
          timeout: Number of seconds to wait for node allocation
          cpus: Number of cpus to allocate
          gpus: Number of gpus to allocate with optional type specifier
                (Examples: "a40:2" for NVIDIA A40, "1" for single GPU)
          mem: Amount of memory to allocate (Examples: "8G" for 8GiB of memory)
          partition: Partition name (see `man salloc` on --partition option for more information)
          account: Account name (see `man salloc` on --account option for more information)
          job_name: Slurm job name displayed in `squeue`

        Returns SubNode object if it has been acquired successfully and None otherwise.
        """
        cmd = ["timeout", str(timeout), "salloc",
                "-J", job_name,
                "--no-shell",
                "-p", partition,
                "-A", account,
                "-t", f"{res_time}:00:00",
                "--mem=" + mem,
                "--gpus=" + gpus,
                "-c", str(cpus)]
        proc = self.run_command(cmd)

        alloc_stat = False
        subnode_job_id = None
        subnode_name = None

        def __reserve_node_irq_handler__(signalNumber, frame):
            """
            Pass SIGINT to subprocess and exit program.
            """
            if self.debug:
                msg = f"reserve_node: Caught signal: {signalNumber}"
                print(msg)
                logging.info(msg)
            proc.send_signal(signal.SIGINT)
            print("Cancelled node allocation. Exiting...")
            exit(1)

        # Stop allocation when  SIGINT (CTRL+C) and SIGTSTP (CTRL+Z) signals
        # are detected.
        signal.signal(signal.SIGINT, __reserve_node_irq_handler__)
        signal.signal(signal.SIGTSTP, __reserve_node_irq_handler__)

        print(f"Allocating node with {cpus} CPU(s), {gpus.split(':').pop()} GPU(s), and {mem} RAM for {res_time} hours...")
        while proc.poll() is None and not alloc_stat:
            print("...")
            line = str(proc.stdout.readline(), 'utf-8').strip()
            if self.debug:
                msg = f"reserve_node: {line}"
                logging.debug(msg)
            if "Pending" in line or "Granted" in line:
                # match against pattern:
                #salloc: Pending job allocation 864875
                #salloc: Granted job allocation 864875
                pattern = re.compile("""
                        (salloc:\s)
                        ((Granted)|(Pending))
                        (\sjob\sallocation\s)
                        (?P<job_id>[0-9]+)
                        """, re.VERBOSE)
                match = pattern.match(line)
                if match is not None:
                    subnode_job_id = match.group("job_id")
            elif "are ready for job" in line:
                # match against pattern:
                #salloc: Nodes n3000 are ready for job
                pattern = re.compile("""
                        (salloc:\sNodes\s)
                        (?P<node_name>[ngz][0-9]{4})
                        (\sare\sready\sfor\sjob)
                        """, re.VERBOSE)
                match = pattern.match(line)
                if match is not None:
                    subnode_name = match.group("node_name")
                    alloc_stat = True
                    break
            elif self.debug:
                msg = f"Skipping line: {line}"
                print(msg)
                logging.debug(msg)
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()

        if not alloc_stat:
            # check if node actually got reserved
            # Background: Sometimes salloc does not print allocated node names
            #             at the end, so we have to check with squeue
            if subnode_job_id is not None:
                tmp_nodes = self.find_nodes(job_name)
                for tmp_node in tmp_nodes:
                    if self.debug:
                        logging.debug(f"reserve_node: fallback: Checking {tmp_node.name} with Job ID {tmp_node.job_id}")
                    if tmp_node.job_id == subnode_job_id:
                        if self.debug:
                            logging.debug(f"reserve_node: fallback: Match found")
                        # get subnode name
                        subnode_name = tmp_node.name
                        break
            else:
                return None
            if subnode_name is None:
                msg = "Error: node allocation timed out."
                print(msg)
                if self.debug:
                    logging.error(msg)
                return None

        assert subnode_job_id is not None
        assert subnode_name is not None
        self.subnode = SubNode(subnode_name, subnode_job_id, self.sing_container, self.xstartup)
        return self.subnode

    def cancel_job(self, job_id:int):
        """
        Cancel specified job ID

        Reference:
            See `man scancel` for more information on usage
        """
        msg = f"Canceling job ID {job_id}"
        print(f"\t{msg}")
        if self.debug:
            logging.debug(msg)
        proc = self.run_command(["scancel", str(job_id)])
        print(str(proc.communicate()[0], 'utf-8'))

    def check_port(self, port:int):
        """
        Returns True if port is unused and False if used.
        """
        if self.debug:
            logging.debug(f"Checking if port {port} is used...")
        cmd = f"netstat -ant | grep LISTEN | grep {port}"
        proc = self.run_command(cmd)
        while proc.poll() is None:
            line = str(proc.stdout.readline(), 'utf-8').strip()
            if self.debug:
                logging.debug(f"netstat line: {line}")
            if str(port) in line:
                return False
        return True

    def get_port(self):
        """
        Returns unused port number if found and None if not found.
        """
        # 300 is arbitrary limit
        for i in range(0,300):
            port = BASE_VNC_PORT + i
            if self.check_port(port):
                return port
        return None

    def create_port_forward(self, login_port:int, subnode_port:int):
        """
        Port forward between login node and subnode

        Args:
          login_port:int : Login node port number
          subnode_port:int : Subnode port number

        Returns True if port forward succeeds and False otherwise.
        """
        assert self.subnode is not None
        assert self.subnode.name is not None
        msg = f"Creating port forward: Login node({login_port})<->Subnode({subnode_port})"
        if self.debug:
            logging.debug(msg)
        else:
            print(f"{msg}...", end="", flush=True)
        cmd = f"ssh -N -f -L {login_port}:127.0.0.1:{subnode_port} {self.subnode.hostname} &> /dev/null"
        status = self.call_command(cmd)

        if status == 0:
            # wait (at most ~20 seconds) until port forward succeeds
            count = 0
            port_used = not self.check_port(login_port)
            while count < 20 and not port_used:
                if self.debug:
                    msg = f"create_port_forward: attempt #{count + 1}: port used? {port_used}"
                    logging.debug(msg)
                port_used = not self.check_port(login_port)
                count += 1
                time.sleep(1)

            if port_used:
                if self.debug:
                    msg = f"Successfully created port forward"
                    logging.info(msg)
                else:
                    print('\x1b[1;32m' + "Success" + '\x1b[0m')
                return True
        if self.debug:
            msg = f"Error: Failed to create port forward"
            logging.error(msg)
        else:
            print('\x1b[1;31m' + "Failed" + '\x1b[0m')
        return False

    def get_port_forwards(self, nodes=None):
        """
        For each node in the SubNodes set `nodes`, get a port map between login
        node port and subnode port, and then fill `vnc_port` and
        `vnc_display_number` subnode attributes if None.

        Example:
          Suppose we have the following VNC sessions (on a single user account):
            n3000 with a login<->subnode port forward from 5900 to 5901,
            n3000 with a login<->subnode port forward from 5901 to 5902,
            n3042 with a login<->subnode port forward from 5903 to 5901.

            This function returns the following:
              { "n3000" : {5901:5900, 5902:5901}, "n3042" : {5901:5903} }

        Args:
          nodes : A set of SubNode objects with names to inspect

        Returns a dictionary with node name as keys and
        LoginNodePort (value) <-> SubNodePort (key) dictionary as value.
        """
        node_port_map = dict()
        if nodes is not None:
            for node in nodes:
                if "(" not in node.name:
                    port_map = dict()
                    cmd = f"ps x | grep ssh | grep {node.name}"
                    proc = self.run_command(cmd)
                    while proc.poll() is None:
                        line = str(proc.stdout.readline(), 'utf-8').strip()
                        if cmd not in line:
                            # Match against pattern:
                            #1974577 ?        Ss     0:20 ssh -N -f -L 5902:127.0.0.1:5902 n3065.hyak.local
                            pattern = re.compile("""
                                    ([^\s]+(\s)+){4}
                                    (ssh\s-N\s-f\s-L\s(?P<ln_port>[0-9]+):127.0.0.1:(?P<sn_port>[0-9]+))
                                    """, re.VERBOSE)
                            match = re.match(pattern, line)
                            if match is not None:
                                ln_port = int(match.group("ln_port"))
                                sn_port = int(match.group("sn_port"))
                                port_map.update({sn_port:ln_port})
                    node_port_map.update({node.name:port_map})
        return node_port_map

    def get_job_port_forward(self, job_id:int, node_name:str, node_port_map:dict):
        """
        Returns tuple containing LoginNodePort and SubNodePort for given job ID
        and node_name. Returns None on failure.
        """
        if self.get_time_left(job_id) is not None:
            port_map = node_port_map[node_name]
            if port_map is not None:
                subnode = SubNode(node_name, job_id, self.sing_container, '', self.debug)
                for vnc_port in port_map.keys():
                    display_number = vnc_port - BASE_VNC_PORT
                    if self.debug:
                        logging.debug(f"get_job_port_forward: Checking job {job_id} vnc_port {vnc_port}")
                    # get PID from VNC pid file
                    pid = subnode.get_vnc_pid(subnode.name, display_number)
                    if pid is None:
                        # try long hostname
                        pid = subnode.get_vnc_pid(subnode.hostname, display_number)
                    # if PID is active, then we have a hit for a specific job
                    if pid is not None and subnode.check_pid(pid):
                        if self.debug:
                            logging.debug(f"get_job_port_forward: {job_id} has vnc_port {vnc_port} and login node port {port_map[vnc_port]}")
                        return (vnc_port,port_map[vnc_port])
        return None

    def get_time_left(self, job_id:int, job_name="vnc"):
        """
        Returns the time remaining for given job ID or None if the job is not
        present.
        """
        cmd = f'squeue -o "%L %.18i %.8j %.8u %R" | grep {os.getlogin()} | grep {job_name} | grep {job_id}'
        proc = self.run_command(cmd)
        if proc.poll() is None:
            line = str(proc.stdout.readline(), 'utf-8')
            return line.split(' ', 1)[0]
        return None

    def print_props(self):
        """
        Print all properties (including subnode properties)
        """
        print("Login node properties:")
        props = vars(self)
        for item in props:
            msg = f"{item} : {props[item]}"
            print(f"\t{msg}")
            if self.debug:
                logging.debug(msg)
            if item == "subnode" and props[item] is not None:
                props[item].print_props()

    def print_status(self, job_name:str, node_set=None, node_port_map=None):
        """
        Print details of each active VNC job in node_set. VNC port and display
        number should be in node_port_map.
        """
        print(f"Active {job_name} jobs:")
        if node_set is not None:
            for node in node_set:
                mapped_port = None
                if node_port_map and node_port_map[node.name]:
                    port_forward = self.get_job_port_forward(node.job_id, node.name, node_port_map)
                    if port_forward:
                        vnc_port = port_forward[0]
                        mapped_port = port_forward[1]
                        node.vnc_display_number = vnc_port + BASE_VNC_PORT
                        node_port_map.get(node.name).pop(vnc_port)
                time_left = self.get_time_left(node.job_id, job_name)
                vnc_active = mapped_port is not None
                ssh_cmd = f"ssh -N -f -L {mapped_port}:127.0.0.1:{mapped_port} {os.getlogin()}@klone.hyak.uw.edu"
                print(f"\tJob ID: {node.job_id}")
                print(f"\t\tSubNode: {node.name}")
                print(f"\t\tTime left: {time_left}")
                print(f"\t\tVNC active: {vnc_active}")
                if vnc_active:
                    print(f"\t\tVNC display number: {vnc_port - BASE_VNC_PORT}")
                    print(f"\t\tVNC port: {vnc_port}")
                    print(f"\t\tMapped LoginNode port: {mapped_port}")
                    print(f"\t\tRun command: {ssh_cmd}")

    def repair_ln_sn_port_forwards(self, node_set=None, node_port_map=None):
        """
        Re-creates port forwards missing from vnc jobs.
        Useful when LoginNode restarts but VNC jobs remain alive.
        """
        if node_set is not None:
            for node in node_set:
                if node_port_map and node_port_map[node.name]:
                    print(f"{node.name} with job ID {node.job_id} already has valid port forward")
                else:
                    subnode = SubNode(node.name, node.job_id, self.sing_container, self.xstartup, self.debug)
                    subnode_pids = subnode.list_pids()
                    # search for vnc process
                    proc = subnode.run_command("ps x | grep vnc")
                    while proc.poll() is None:
                        line = str(proc.stdout.readline(), 'utf-8').strip()
                        pid = int(line.split(' ', 1)[0])
                        # match found
                        if pid in subnode_pids:
                            if self.debug:
                                logging.debug(f"repair_ln_sn_port_forwards: VNC PID {pid} found for job ID {node.job_id}")
                            pattern = re.compile("""
                                    (vnc\s+:)
                                    (?P<display_number>\d+)
                                    """, re.VERBOSE)
                            match = re.search(pattern, line)
                            assert match is not None
                            vnc_port = BASE_VNC_PORT + int(match.group("display_number"))
                            u2h_port = self.get_port()
                            if self.debug:
                                logging.debug(f"repair_ln_sn_port_forwards: LoginNode({u2h_port})<->JobID({vnc_port})")
                            if u2h_port is None:
                                print(f"Error: cannot find available/unused port")
                                continue
                            else:
                                self.subnode = subnode
                                self.create_port_forward(u2h_port, vnc_port)
