# -*- coding: utf-8 -*-

import sys
import os.path
import subprocess
import json
import uuid
import string
import tempfile
import atexit
import shutil
from xml.etree import ElementTree
import charon.backends


class Deployment:
    """Charon top-level deployment manager."""

    def __init__(self, state_file, create=False, nix_exprs=[]):
        self.state_file = state_file
        self.machines = { }
        self.configs_path = None
        self.description = "Unnamed Charon network"
        
        self.expr_path = os.path.dirname(__file__) + "/../../../../share/nix/charon"
        if not os.path.exists(self.expr_path):
            self.expr_path = os.path.dirname(__file__) + "/../nix"

        if create:
            if os.path.exists(self.state_file):
                self.load_state()
            else:
                self.uuid = uuid.uuid1()
            self.nix_exprs = nix_exprs
        else:
            self.load_state()

        self.tempdir = tempfile.mkdtemp(prefix="charon-tmp")
        atexit.register(lambda: shutil.rmtree(self.tempdir))
            

    def load_state(self):
        """Read the current deployment state from the state file."""
        f = open(self.state_file, 'r')
        state = json.load(f)
        self.nix_exprs = state['networkExprs']
        self.uuid = uuid.UUID(state['uuid'])
        self.description = state.get('description', self.description)
        self.machines = { }
        self.configs_path = state.get('vmsPath', None)
        for n, v in state['machines'].iteritems():
            self.machines[n] = charon.backends.create_state(self, v['targetEnv'], n)
            self.machines[n].deserialise(v)
        
            
    def write_state(self):
        """Write the current deployment state to the state file in JSON format."""
        machines = {}
        for m in self.machines.itervalues():
            if not m.created: continue
            x = m.serialise()
            x["targetEnv"] = m.get_type()
            machines[m.name] = x
        state = {'networkExprs': self.nix_exprs,
                 'uuid': str(self.uuid),
                 'description': self.description,
                 'machines': machines}
        if self.configs_path: state['vmsPath'] = self.configs_path
        tmp = self.state_file + ".tmp"
        f = open(tmp, 'w')
        json.dump(state, f, indent=2)
        f.close()
        os.rename(tmp, self.state_file)


    def evaluate(self):
        """Evaluate the Nix expressions belonging to this deployment into a deployment model."""

        self.definitions = {}

        try:
            xml = subprocess.check_output(
                ["nix-instantiate", "-I", "charon=" + self.expr_path,
                 "--eval-only", "--show-trace", "--xml", "--strict",
                 "<charon/eval-machine-info.nix>",
                 "--arg", "networkExprs", "[ " + string.join(self.nix_exprs) + " ]",
                 "-A", "info"])
        except subprocess.CalledProcessError:
            raise NixEvalError

        tree = ElementTree.fromstring(xml)

        # Extract global deployment attributes.
        info = tree.find("attrs/attr[@name='network']")
        assert info != None
        elem = info.find("attrs/attr[@name='description']/string")
        if elem != None: self.description = elem.get("value")

        # Extract machine information.
        machines = tree.find("attrs/attr[@name='machines']/attrs")

        for m in machines.findall("attr"):
            defn = charon.backends.create_definition(m)
            self.definitions[defn.name] = defn


    def get_physical_spec(self):
        """Compute the contents of the Nix expression specifying the computed physical deployment attributes"""

        def for_machine(m):
            lines = []
            lines.append("  " + m.name + " = { config, pkgs, ... }: {")
            lines.extend(m.get_physical_spec(self.active))
            private_ipv4 = m.private_ipv4
            if private_ipv4: lines.append('    networking.privateIPv4 = "{0}";'.format(private_ipv4))
            public_ipv4 = m.public_ipv4
            if public_ipv4: lines.append('    networking.publicIPv4 = "{0}";'.format(public_ipv4))
            hosts = []
            for m2 in self.active.itervalues():
                if m == m2: continue
                ip = m.address_to(m2)
                if ip: hosts.append("{0} {1}".format(ip, m2.name))
            lines.append('    networking.extraHosts = "{0}\\n";'.format('\\n'.join(hosts)))
            lines.append('    boot.kernelModules = [ "tun" ];')
            lines.append("  };\n")
            return "\n".join(lines)

        return "".join(["{\n"] + [for_machine(m) for m in self.active.itervalues()] + ["}\n"])
            

    def build_configs(self, dry_run=False):
        """Build the machine configurations in the Nix store."""

        print >> sys.stderr, "building all machine configurations..."

        phys_expr = self.tempdir + "/physical.nix"
        f = open(phys_expr, "w")
        f.write(self.get_physical_spec())
        f.close()
        
        try:
            configs_path = subprocess.check_output(
                ["nix-build", "-I", "charon=" + self.expr_path, "--show-trace",
                 "<charon/eval-machine-info.nix>",
                 "--arg", "networkExprs", "[ " + " ".join(self.nix_exprs + [phys_expr]) + " ]",
                 "-A", "machines", "-o", self.tempdir + "/configs"]
                + (["--dry-run"] if dry_run else [])).rstrip()
        except subprocess.CalledProcessError:
            raise Exception("unable to build all machine configurations")

        return configs_path
        

    def copy_closure(self, m, toplevel):
        """Copy a closure to the corresponding machine."""
        
        # !!! Implement copying between cloud machines, as in the Perl
        # version.

        env = dict(os.environ)
        env['NIX_SSHOPTS'] = ' '.join(m.get_ssh_flags());
        res = subprocess.Popen(
            ["nix-copy-closure", "--gzip", "--to", "root@" + m.get_ssh_name(), toplevel],
            env=env).wait()
        if res != 0:
            raise Exception("unable to copy closure to machine ‘{0}’".format(m.name))


    def copy_closures(self, configs_path, include, exclude):
        """Copy the closure of each machine configuration to the corresponding machine."""

        for m in self.active.itervalues():
            if not should_do(m, include, exclude): continue
            print >> sys.stderr, "copying closure to machine ‘{0}’...".format(m.name)
            m.new_toplevel = os.path.realpath(configs_path + "/" + m.name)
            if not os.path.exists(m.new_toplevel):
                raise Exception("can't find closure of machine ‘{0}’".format(m.name))
            self.copy_closure(m, m.new_toplevel)


    def activate_configs(self, configs_path, include, exclude):
        """Activate the new configuration on a machine."""

        for m in self.active.itervalues():
            if not should_do(m, include, exclude): continue
            
            print >> sys.stderr, "activating new configuration on machine ‘{0}’...".format(m.name)

            res = subprocess.call(
                ["ssh", "-x", "root@" + m.get_ssh_name()]
                + m.get_ssh_flags() +
                [# Set the system profile to the new configuration.
                 "nix-env -p /nix/var/nix/profiles/system --set " + m.new_toplevel + ";" +
                 # Run the switch script.  This will also update the
                 # GRUB boot loader.  For performance, skip this step
                 # if the new config is already current.
                 "cur=$(readlink /var/run/current-system); " +
                 'if [ "$cur" != ' + m.new_toplevel + " ]; then /nix/var/nix/profiles/system/bin/switch-to-configuration switch; fi"])
            if res != 0: raise Exception("unable to activate new configuration on machine ‘{0}’".format(m.name))

            # Record that we switched this machine to the new
            # configuration.
            m.cur_configs_path = configs_path
            m.cur_toplevel = m.new_toplevel
            self.write_state()


    def deploy(self, dry_run=False, build_only=False, create_only=False,
               include=[], exclude=[], check=False):
        """Perform the deployment defined by the deployment model."""

        self.evaluate()

        # Create state objects for all defined machines.
        for m in self.definitions.itervalues():
            if m.name not in self.machines:
                self.machines[m.name] = charon.backends.create_state(self, m.get_type(), m.name)

        # Determine the set of active machines.  (We can't just delete
        # obsolete machines from ‘self.machines’ because they contain
        # important state that we don't want to forget about.)
        self.active = {}
        for m in self.machines.itervalues():
            if m.name in self.definitions:
                self.active[m.name] = m
            else:
                print >> sys.stderr, "machine ‘{0}’ is obsolete".format(m.name)
                if not should_do(m, include, exclude): continue
                # !!! If kill_obsolete is set, kill the machine.

        # Start or update the active machines.  !!! Should do this in parallel.
        if not dry_run and not build_only:
            for m in self.active.itervalues():
                if not should_do(m, include, exclude): continue
                m.created = True
                m.create(self.definitions[m.name], check=check)

        if create_only: return

        # Build the machine configurations.
        if dry_run:
            self.build_configs(dry_run=True)
            return

        self.configs_path = self.build_configs()

        # Record configs_path in the state so that the ‘info’ command
        # can show whether machines have an outdated configuration.
        self.write_state()

        if build_only: return
        
        # Copy the closures of the machine configurations to the
        # target machines.
        self.copy_closures(self.configs_path, include=include, exclude=exclude)

        # Active the configurations.
        self.activate_configs(self.configs_path, include=include, exclude=exclude)

            
    def destroy_vms(self):
        """Destroy all current or obsolete VMs."""

        for m in self.machines.values(): # don't use itervalues() here
            m.destroy()
            del self.machines[m.name]
            self.write_state()
        

class NixEvalError(Exception):
    pass


def should_do(m, include, exclude):
    if m.name in exclude: return False
    if include == []: return True
    return m.name in include
