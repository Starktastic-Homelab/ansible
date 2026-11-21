"""Offline reads of the k3s_common role: never connects to or configures a host."""
from pathlib import Path
import unittest

from ansible.parsing.dataloader import DataLoader

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


if __name__ == "__main__":
    unittest.main()
