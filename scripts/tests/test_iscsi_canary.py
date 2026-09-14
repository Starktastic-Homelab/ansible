#!/usr/bin/env python3
"""Offline regression checks for the isolated iSCSI rebuild entry point."""

import base64
import configparser
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
CANARIES = ROOT / "canaries"


def walk_tasks(tasks, directory):
    for task in tasks:
        yield task
        if "ansible.builtin.import_tasks" in task:
            path = directory / task["ansible.builtin.import_tasks"]
            yield from walk_tasks(yaml.safe_load(path.read_text()), path.parent)
        for section in ("block", "rescue", "always"):
            yield from walk_tasks(task.get(section, []), directory)


class InitExtractionTests(unittest.TestCase):
    def test_production_init_expands_to_the_original_task_tree(self):
        main = yaml.safe_load((ROOT / "roles/k3s_init/tasks/main.yml").read_text())
        install = ROOT / "roles/k3s_init/tasks/install.yml"
        self.assertTrue(install.is_file(), "The isolated init-only task entry is missing")
        self.assertEqual(
            set(main[0]), {"name", "ansible.builtin.import_tasks"},
            "The static import must not add conditions, tags, or other behavior",
        )
        self.assertEqual(main[0]["ansible.builtin.import_tasks"], "install.yml")
        expanded = yaml.safe_load(install.read_text()) + main[1:]
        # Canonical task tree from 9212ea1, including block/rescue, tags and when.
        self.assertEqual(
            hashlib.sha256(json.dumps(expanded, sort_keys=True).encode()).hexdigest(),
            "0051257f88fc506d6751e85e3e99c4d24caa6936f34ea69770f8abf0915aa1ec",
        )


class CanaryTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(
            (CANARIES / "iscsi-rebuild.yml").is_file(),
            "The isolated canary playbook is missing",
        )
        self.plays = yaml.safe_load((CANARIES / "iscsi-rebuild.yml").read_text())
        self.play = self.plays[-1]
        self.tasks = list(walk_tasks(
            self.play.get("pre_tasks", []) + self.play["tasks"], CANARIES
        ))

    def test_only_the_single_canary_uses_the_init_only_entry(self):
        self.assertEqual([p["hosts"] for p in self.plays],
                         ["localhost", "iscsi_canary"])
        self.assertEqual(self.plays[0]["connection"], "local")
        self.assertFalse(self.plays[0]["gather_facts"])
        self.assertFalse(self.plays[0]["become"])
        self.assertTrue(all("ansible.builtin.assert" in t
                            for t in self.plays[0]["tasks"]))
        self.assertEqual(self.play["hosts"], "iscsi_canary")
        self.assertFalse(self.play["gather_facts"])
        self.assertNotIn("roles", self.play)
        roles = [
            task["ansible.builtin.import_role"]
            for task in self.tasks if "ansible.builtin.import_role" in task
        ]
        self.assertEqual(roles, [{"name": "k3s_init", "tasks_from": "install"}])
        self.assertEqual(
            yaml.safe_load((ROOT / "roles/k3s_init/meta/main.yml").read_text()),
            {"dependencies": [{"role": "k3s_common"}]},
        )
        text = json.dumps(self.tasks)
        for forbidden in ("kube-vip.yml", "bootstrap_cluster", "k3s.yml", "node-token"):
            self.assertNotIn(forbidden, text)
        self.assertNotIn("ansible.builtin.include_role", text)
        self.assertLess(
            next(i for i, t in enumerate(self.tasks)
                 if t["name"] == "Verify disposable VM hostname"),
            next(i for i, t in enumerate(self.tasks) if t.get("become") is True),
        )

    def test_supported_network_dropin_and_no_registry_credentials(self):
        self.assertEqual(self.play["vars"]["vip_address"], "{{ ansible_host }}")
        self.assertIs(self.play["vars"]["k3s_common_registry_auth_enabled"], False)
        dropin = next(t["ansible.builtin.copy"] for t in self.tasks
                      if t.get("ansible.builtin.copy", {}).get("dest")
                      == "/etc/rancher/k3s/config.yaml.d/10-iscsi-canary.yaml")
        self.assertEqual(yaml.safe_load(dropin["content"]), {
            "cluster-cidr": "10.242.0.0/16",
            "service-cidr": "10.243.0.0/16",
            "cluster-dns": "10.243.0.10",
        })
        defaults = yaml.safe_load(
            (ROOT / "roles/k3s_common/defaults/main.yml").read_text()
        )
        self.assertIs(defaults["k3s_common_registry_auth_enabled"], True)
        common = yaml.safe_load((ROOT / "roles/k3s_common/tasks/main.yml").read_text())
        registry = next(t for t in common if "ansible.builtin.template" in t)
        self.assertEqual(registry["when"], "k3s_common_registry_auth_enabled | bool")

    def test_configuration_disables_implicit_inventory_and_vault_loading(self):
        config = configparser.ConfigParser()
        config.read(CANARIES / "ansible.cfg")
        self.assertEqual(config["defaults"]["inventory"], "/dev/null")
        self.assertEqual(config["defaults"]["vars_plugins_enabled"], "")
        self.assertEqual(config["defaults"]["roles_path"], "../roles")
        self.assertNotIn("vault_password_file", config["defaults"])
        self.assertEqual(config["inventory"]["enable_plugins"], "yaml,ini")

    def test_kubeconfig_is_fetched_privately_and_not_to_the_default_context(self):
        fetch = next(t for t in self.tasks
                     if t.get("ansible.builtin.slurp", {}).get("src")
                     == "/etc/rancher/k3s/k3s.yaml")
        self.assertIs(fetch["no_log"], True)
        export = next(t for t in self.tasks
                      if t.get("ansible.builtin.copy", {}).get("dest")
                      == "{{ canary_state_dir }}/kubeconfig")
        self.assertIs(export["no_log"], True)
        self.assertIs(export["diff"], False)
        self.assertEqual(export["ansible.builtin.copy"]["mode"], "0600")
        self.assertEqual(export["delegate_to"], "localhost")
        self.assertIs(export["become"], False)
        self.assertIn("ansible_host", export["ansible.builtin.copy"]["content"])
        self.assertTrue(any(
            t.get("ansible.builtin.copy", {}).get("dest")
            == "{{ canary_state_dir }}/node-identity.json" for t in self.tasks
        ))

    def test_iscsi_never_logs_out_or_restarts_an_unchanged_active_identity(self):
        tasks = yaml.safe_load((CANARIES / "tasks/iscsi.yml").read_text())
        text = json.dumps(tasks)
        self.assertNotIn("logout", text)
        query = next(t for t in tasks if "ansible.builtin.command" in t)
        self.assertEqual(query["ansible.builtin.command"]["argv"],
                         ["iscsiadm", "--mode", "session"])
        self.assertEqual(query["failed_when"], "canary_iscsi_sessions.rc not in [0, 21]")
        identity = next(t for t in tasks if "ansible.builtin.copy" in t)
        self.assertEqual(identity["when"], "canary_current_iqn != canary_iscsi_iqn")
        service = next(t["ansible.builtin.systemd_service"] for t in tasks
                       if "ansible.builtin.systemd_service" in t)
        self.assertTrue(service["enabled"])
        self.assertEqual(service["state"],
                         "{{ 'restarted' if canary_current_iqn != canary_iscsi_iqn else 'started' }}")


class GuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = ROOT / ".canary-checks" / f"regression-{os.getpid()}"
        cls.workspace.mkdir(parents=True)
        cls.state = cls.workspace / "state"
        cls.state.mkdir(mode=0o700)
        for name in ("local", "remote"):
            (cls.workspace / name).mkdir()
        cls.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("ANSIBLE_")
        }
        cls.env.update(
            ANSIBLE_CONFIG=str(CANARIES / "ansible.cfg"),
            ANSIBLE_LOCAL_TEMP=str(cls.workspace / "local"),
            ANSIBLE_REMOTE_TEMP=str(cls.workspace / "remote"),
            ANSIBLE_NOCOLOR="1",
            # Ansible's local RPC socket must fit Linux's 108-byte path limit.
            TMPDIR=str(ROOT),
            KUBECONFIG="",
        )
        cls.base = {
            "canary_fixture_id": "test1",
            "canary_node_name": "iscsi-rebuild-canary-test1",
            "canary_iscsi_iqn": "iqn.2026-09.arpa.example:iscsi-rebuild-canary-test1",
            "canary_state_dir": str(cls.state),
            "k3s_version": "v1.36.4+k3s1",
            "flannel_iface": "eth1",
            "ansible_host": "192.0.2.10",
            "ansible_user": "offline-canary",
            "ansible_private_key_file": str(cls.workspace / "unused-test-key"),
            "vip_address": "192.0.2.10",
            "k3s_common_registry_auth_enabled": False,
            "canary_actual_hostname": {"stdout": "iscsi-rebuild-canary-test1"},
        }

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.workspace)

    def run_guards(self, overrides=None, *, expect=True, tasks=None,
                   hosts=None, masters=None, extra_hosts=None, env=None):
        self.assertTrue((CANARIES / "tasks/preflight.yml").is_file(),
                        "The executable preflight guards are missing")
        hosts = ["canary"] if hosts is None else hosts
        masters = hosts if masters is None else masters
        inventory = {"all": {"children": {
            "iscsi_canary": {"hosts": {
                host: {"ansible_connection": "local",
                       "ansible_python_interpreter": sys.executable} for host in hosts
            }},
            "masters": {"hosts": dict.fromkeys(masters, {})},
        }, "hosts": dict.fromkeys(extra_hosts or [], {})}}
        variables = self.base | (overrides or {})
        if tasks is None:
            plays = yaml.safe_load((CANARIES / "iscsi-rebuild.yml").read_text())
            play = plays[-1]
            hostname_guard = next(t for t in play["pre_tasks"]
                                  if t["name"] == "Verify disposable VM hostname")
            tasks = [
                {"ansible.builtin.import_tasks": str(CANARIES / "tasks/preflight.yml")},
                hostname_guard,
            ]
        # Only selected controller checks/copies execute; never a role or SSH.
        harness = [{
            "name": "Offline canary guards",
            "hosts": "iscsi_canary",
            "gather_facts": False,
            "become": False,
            "vars": variables,
            "tasks": tasks,
        }]
        plays = yaml.safe_load((CANARIES / "iscsi-rebuild.yml").read_text())
        if plays[0]["hosts"] == "localhost":
            harness.insert(0, plays[0])
        (self.workspace / "inventory.yml").write_text(yaml.safe_dump(inventory))
        (self.workspace / "guards.yml").write_text(yaml.safe_dump(harness))
        result = subprocess.run(
            ["ansible-playbook", "-i", str(self.workspace / "inventory.yml"),
             str(self.workspace / "guards.yml")],
            cwd=ROOT, env=self.env | (env or {}), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60,
            check=False,
        )
        if expect:
            self.assertEqual(result.returncode, 0, result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn('"assertion":', result.stdout,
                          "Failure must be a guard assertion, not a tool/SSH error")
        return result.stdout

    def test_valid_contract_passes_without_ssh(self):
        self.run_guards()

    def test_explicit_connection_inputs_are_required(self):
        for name in ("ansible_user", "ansible_private_key_file"):
            for value in (None, ""):
                with self.subTest(name=name, value=value):
                    self.run_guards({name: value}, expect=False)

    def test_invalid_contracts_fail_before_provisioning(self):
        cases = [
            {"canary_fixture_id": "Test1"},
            {"canary_fixture_id": "../test1"},
            {"canary_node_name": "production-master"},
            {"canary_node_name": "iscsi-rebuild-canary-other"},
            {"canary_actual_hostname": {"stdout": "production-master"}},
            {"k3s_version": "v1.35.0+k3s1"},
            {"canary_iscsi_iqn": "iqn.invalid\nInitiatorName=shared"},
            {"flannel_iface": "eth1;true"},
            {"ansible_host": "999.0.0.1"},
            {"canary_state_dir": "relative"},
            {"canary_state_dir": str(self.state / ".." / "state")},
            {"canary_state_dir": "/"},
            {"canary_state_dir": str(Path.home() / ".kube")},
            {"vip_address": "192.0.2.99"},
            {"k3s_common_registry_auth_enabled": True},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.run_guards(overrides, expect=False)
        self.run_guards(hosts=["canary", "second"], expect=False)
        self.run_guards(extra_hosts=["production"], expect=False)
        self.run_guards(masters=[], expect=False)

    def test_empty_inventory_fails_instead_of_silently_skipping(self):
        self.run_guards(hosts=[], expect=False)

    def test_trailing_newlines_and_ambiguous_ip_addresses_fail(self):
        for overrides in (
            {"canary_iscsi_iqn": self.base["canary_iscsi_iqn"] + "\n"},
            {"flannel_iface": "eth1\n"},
            {"ansible_host": "192.0.2.10\n", "vip_address": "192.0.2.10\n"},
            {"ansible_host": "192.0.02.10", "vip_address": "192.0.02.10"},
        ):
            with self.subTest(overrides=overrides):
                self.run_guards(overrides, expect=False)

    def test_exports_are_private_rewritten_and_never_logged(self):
        marker = "SYNTHETIC-CANARY-TEST-KEY-DO-NOT-LOG"
        config = {
            "apiVersion": "v1", "kind": "Config",
            "clusters": [{"name": "default", "cluster": {
                "server": "https://127.0.0.1:6443",
                "certificate-authority-data": "synthetic-test-ca",
            }}],
            "users": [{"name": "default", "user": {"client-key-data": marker}}],
        }
        machine_id = "0123456789abcdef" * 2
        boot_id = "01234567-89ab-cdef-0123-456789abcdef"

        def slurp(content):
            return {"content": base64.b64encode(content.encode()).decode()}

        tasks = [t for t in yaml.safe_load(
            (CANARIES / "tasks/export.yml").read_text()
        ) if "ansible.builtin.slurp" not in t]
        output = self.run_guards({
            "canary_kubeconfig_slurp": slurp(yaml.safe_dump(config)),
            "canary_machine_id": slurp(machine_id + "\n"),
            "canary_boot_id": slurp(boot_id + "\n"),
        }, tasks=tasks)
        exported = yaml.safe_load((self.state / "kubeconfig").read_text())
        self.assertEqual(exported["clusters"][0]["cluster"]["server"],
                         "https://192.0.2.10:6443")
        self.assertEqual(exported["users"], config["users"])
        self.assertEqual(json.loads((self.state / "node-identity.json").read_text()), {
            "fixture_id": self.base["canary_fixture_id"],
            "node_name": self.base["canary_node_name"],
            "machine_id": machine_id, "boot_id": boot_id,
        })
        for filename in ("kubeconfig", "node-identity.json"):
            self.assertEqual((self.state / filename).stat().st_mode & 0o777, 0o600)
        self.assertNotIn(marker, output)

    def test_destination_symlinks_hardlinks_permissions_and_active_config_fail(self):
        link = self.workspace / "linked-state"
        link.symlink_to(self.state, target_is_directory=True)
        self.run_guards({"canary_state_dir": str(link)}, expect=False)
        self.state.chmod(0o755)
        self.run_guards(expect=False)
        self.state.chmod(0o700)
        source = self.workspace / "untouched"
        source.write_text("must not change\n")
        for name in ("kubeconfig", "node-identity.json"):
            output = self.state / name
            output.symlink_to(source)
            self.run_guards(expect=False)
            output.unlink()
            os.link(source, output)
            self.run_guards(expect=False)
            output.unlink()
        self.run_guards(
            env={"KUBECONFIG": str(self.state / "kubeconfig")}, expect=False
        )
        self.assertEqual(source.read_text(), "must not change\n")

    def test_active_sessions_require_the_same_initiator(self):
        self.assertTrue((CANARIES / "tasks/iscsi.yml").is_file(),
                        "The initiator preparation tasks are missing")
        tasks = [task for task in yaml.safe_load(
            (CANARIES / "tasks/iscsi.yml").read_text()
        ) if "ansible.builtin.assert" in task]
        self.assertTrue(tasks, "An active-session identity guard is required")
        for current, rc, accepted in (
            (self.base["canary_iscsi_iqn"], 0, True),
            ("iqn.2026-09.arpa.example:other", 0, False),
            ("", 0, False),
            ("iqn.2026-09.arpa.example:other", 21, True),
            ("", 21, True),
        ):
            with self.subTest(current=current, rc=rc):
                self.run_guards(
                    {"canary_current_iqn": current, "canary_iscsi_sessions": {"rc": rc}},
                    tasks=tasks, expect=accepted,
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
