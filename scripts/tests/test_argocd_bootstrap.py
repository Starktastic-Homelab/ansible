"""Offline reads of the bootstrap_cluster ArgoCD install: never connects to a cluster."""
from pathlib import Path
import unittest

from ansible.parsing.dataloader import DataLoader

ROOT = Path(__file__).resolve().parents[2]


def install_values():
    tasks = DataLoader().load_from_file(str(ROOT / "roles/bootstrap_cluster/tasks/argocd.yml"), trusted_as_template=True)
    task = next(t for t in tasks if t["name"] == "Install ArgoCD")
    return task["kubernetes.core.helm"]["release_values"]


class ArgoCdBootstrapTests(unittest.TestCase):
    def test_kustomize_helm_inflation(self):
        # apps democratic-csi builds its chart through kustomize helmCharts (ADR 0017, F14).
        cm = install_values()["configs"]["cm"]
        self.assertEqual(cm["kustomize.buildOptions"], "--enable-helm")
        for key in ("url", "accounts.homepage", "oidc.config"):
            self.assertIn(key, cm)


if __name__ == "__main__":
    unittest.main()
