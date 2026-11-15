"""Offline reads of the k3s_common role: never connects to or configures a host."""
from pathlib import Path
import unittest

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

ROOT = Path(__file__).resolve().parents[2]


def read(path):
    return DataLoader().load_from_file(str(ROOT / path), trusted_as_template=True)


def common_tasks():
    return read("roles/k3s_common/tasks/main.yml")


def find(tasks, module, **fields):
    for index, task in enumerate(tasks):
        body = task.get(module)
        if isinstance(body, dict) and all(body.get(k) == v for k, v in fields.items()):
            return index, task
    raise AssertionError(f"no {module} task with {fields}")


class StorageSettingsTests(unittest.TestCase):
    def test_kubelet_dropin(self):
        _, task = find(common_tasks(), "ansible.builtin.copy",
                       dest="/var/lib/rancher/k3s/agent/etc/kubelet.conf.d/50-graceful-shutdown.conf")
        lines = task["ansible.builtin.copy"]["content"].splitlines()
        for line in ("apiVersion: kubelet.config.k8s.io/v1beta1", "kind: KubeletConfiguration",
                     "shutdownGracePeriod: 90s", "shutdownGracePeriodCriticalPods: 40s"):
            self.assertIn(line, lines)
        config = next(t for t in common_tasks() if t["name"] == "Write the K3s node config")
        self.assertNotIn("shutdownGracePeriod", config["ansible.builtin.copy"]["content"])

    def test_iqn_written_before_install(self):
        tasks = common_tasks()
        iqn, task = find(tasks, "ansible.builtin.copy", dest="/etc/iscsi/initiatorname.iscsi")
        self.assertEqual(task["ansible.builtin.copy"]["content"],
                         "InitiatorName=iqn.2026-11.net.starktastic:{{ inventory_hostname }}\n")
        self.assertNotIn("when", task)  # every k3s node, the lab server included (R17)
        # copy does not create parents, and open-iscsi is not installed yet.
        mkdir, _ = find(tasks, "ansible.builtin.file", path="/etc/iscsi", state="directory")
        self.assertLess(mkdir, iqn)
        apt, _ = find(tasks, "ansible.builtin.apt", name="open-iscsi")
        download, _ = find(tasks, "ansible.builtin.get_url", dest="/tmp/k3s-install.sh")
        self.assertLess(iqn, apt)
        self.assertLess(apt, download)
        # Every installing role depends on k3s_common, so it runs first.
        for role in ("k3s_init", "k3s_masters", "k3s_workers"):
            deps = read(f"roles/{role}/meta/main.yml")["dependencies"]
            self.assertIn("k3s_common", [d if isinstance(d, str) else d.get("role") for d in deps])

    def test_iscsid_socket_and_manual_startup(self):
        tasks = common_tasks()
        find(tasks, "ansible.builtin.systemd", name="iscsid.socket", enabled=True)
        _, task = find(tasks, "ansible.builtin.lineinfile", path="/etc/iscsi/iscsid.conf")
        self.assertEqual(task["ansible.builtin.lineinfile"]["line"], "node.startup = manual")

    def test_kcm_flag_masters_only(self):
        variables = read("roles/k3s_common/defaults/main.yml")
        config = next(t for t in common_tasks() if t["name"] == "Write the K3s node config")
        content = config["ansible.builtin.copy"]["content"]
        for groups, expected in ((["masters"], True), (["workers"], False)):
            variables["group_names"] = groups
            rendered = Templar(variables=variables).template(trust_as_template(content))
            self.assertEqual("disable-force-detach-on-timeout=true" in rendered, expected)
        self.assertEqual(Templar(variables=variables).template(
            trust_as_template("{{ k3s_common_kube_controller_manager_args }}"))[-1],
            "disable-force-detach-on-timeout=true")

    def test_fstrim_timer_enabled(self):
        find(common_tasks(), "ansible.builtin.systemd", name="fstrim.timer", enabled=True)

    def test_no_multipath(self):
        for path in ("roles/k3s_common/tasks/main.yml", "roles/k3s_workers/tasks/main.yml"):
            self.assertNotIn("multipath", (ROOT / path).read_text().lower())

    def test_persistent_journal(self):
        _, task = find(common_tasks(), "ansible.builtin.file", path="/var/log/journal")
        self.assertEqual(task["ansible.builtin.file"]["state"], "directory")


if __name__ == "__main__":
    unittest.main()
