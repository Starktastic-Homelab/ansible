"""Offline rendering only: never connects to or configures a host."""
from pathlib import Path
import unittest

import yaml
from ansible.template import Templar, trust_as_template
from ansible.parsing.dataloader import DataLoader
from ansible.playbook.task import Task
from ansible.playbook.play import Play
from ansible.playbook.block import Block

ROOT = Path(__file__).resolve().parents[2]


def read(path):
    return DataLoader().load_from_file(str(ROOT / path), trusted_as_template=True)


class BootstrapTests(unittest.TestCase):
    def variables(self, enabled=False, **overrides):
        variables = read("roles/k3s_common/defaults/main.yml")
        settings = ROOT / "group_vars/all/proxmox_csi.yml"
        if settings.exists():
            variables.update(read("group_vars/all/proxmox_csi.yml"))
        variables.update(group_names=["workers"], proxmox_csi_enabled=enabled,
                         proxmox_csi_region="homelab", proxmox_node="pve")
        variables.update(overrides)
        return variables

    def render_config(self, variables):
        task = next(task for task in read("roles/k3s_common/tasks/main.yml")
                    if task["name"] == "Write the K3s node config")
        templar = Templar(variables=variables)
        return templar.template(trust_as_template(task["ansible.builtin.copy"]["content"]))

    def test_disabled_preserves_exact_config_bytes(self):
        for groups in (["workers"], ["masters"]):
            expected = '---\nkubelet-arg:\n  - "resolv-conf=/etc/k3s-resolv.conf"\n'
            if "masters" in groups:
                for component in ("kube-controller-manager", "kube-cloud-controller-manager", "kube-scheduler"):
                    expected += component + '-arg:\n  - "leader-elect-lease-duration=60s"\n'
                    expected += '  - "leader-elect-renew-deadline=45s"\n  - "leader-elect-retry-period=10s"\n'
            with self.subTest(groups=groups):
                self.assertEqual(self.render_config(self.variables(group_names=groups)), expected)

    def test_new_nodes_get_inventory_topology(self):
        for groups in (["workers"], ["masters"]):
            for node in ("pve", "pve-02"):
                with self.subTest(groups=groups, node=node):
                    config = yaml.safe_load(self.render_config(self.variables(
                        True, group_names=groups, proxmox_node=node)))
                    self.assertEqual(set(config["node-label"]), {
                        "topology.kubernetes.io/region=homelab",
                        "topology.kubernetes.io/zone=" + node})

    def test_invalid_topology_fails_before_host_configuration(self):
        tasks = read("roles/k3s_common/tasks/main.yml")
        # The first enabled operation is validation, before any host write.
        task = tasks[0]
        expressions = task["ansible.builtin.assert"]["that"]
        for overrides in ({"proxmox_csi_region": ""}, {"proxmox_node": ""},
                          {"proxmox_node": "bad/node"}, {"proxmox_csi_region": "a" * 64},
                          {"proxmox_node": "pve\nnode-label: injected"}, {"proxmox_node": None}):
            templar = Templar(variables=self.variables(True, **overrides))
            with self.subTest(overrides=overrides):
                self.assertFalse(all(templar.evaluate_expression(trust_as_template(expr))
                                     for expr in expressions))
        templar = Templar(variables=self.variables(True))
        self.assertTrue(all(templar.evaluate_expression(trust_as_template(expr))
                            for expr in expressions))

    def test_existing_nodes_receive_the_same_host_specific_labels(self):
        tasks = read("roles/bootstrap_cluster/tasks/proxmox-csi.yml")
        patch = next(task["kubernetes.core.k8s"] for task in tasks if "kubernetes.core.k8s" in task)
        self.assertEqual(patch["state"], "patched")
        hostvars = {}
        for name, node in (("worker-01", "pve"), ("worker-02", "pve-02")):
            hostvars[name] = Templar(variables=self.variables(True, proxmox_node=node)).template(
                read("group_vars/all/proxmox_csi.yml")["proxmox_csi_node_labels"])
        for name, labels in hostvars.items():
            templar = Templar(variables={"item": name, "hostvars": {
                host: {"proxmox_csi_node_labels": values} for host, values in hostvars.items()}})
            self.assertEqual(templar.template(patch["name"]), name)
            self.assertEqual(templar.template(patch["definition"])["metadata"]["labels"], labels)
        self.assertNotEqual(hostvars["worker-01"], hostvars["worker-02"])

    def test_bootstrap_tags_select_the_included_operations(self):
        play = next(play for play in read("k3s.yml") if play["name"] == "Bootstrap cluster")
        include = read("roles/bootstrap_cluster/tasks/main.yml")[0]
        parent = Play()
        parent.tags = list(play["tags"])
        for selector in include["tags"]:
            for operation in read("roles/bootstrap_cluster/tasks/proxmox-csi.yml"):
                task = Task(block=Block(play=parent))
                # Dynamic includes do not pass their own tags to child tasks.
                task.tags = list(operation.get("tags", []))
                with self.subTest(selector=selector, operation=operation["name"]):
                    self.assertTrue(task.evaluate_tags([selector], [], {}))

    def test_source_defaults_leave_feature_disabled(self):
        self.assertIs(read("group_vars/all/proxmox_csi.yml")["proxmox_csi_enabled"], False)
        task = read("roles/k3s_common/tasks/main.yml")[0]
        self.assertFalse(Templar(variables=self.variables()).evaluate_expression(
            trust_as_template(task["when"])))
        task = read("roles/bootstrap_cluster/tasks/main.yml")[0]
        self.assertFalse(Templar(variables=self.variables()).evaluate_expression(
            trust_as_template(task["when"])))


if __name__ == "__main__":
    unittest.main()
