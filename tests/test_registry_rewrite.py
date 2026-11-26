"""Rewriting the registry keeps its comments, and `new` saves in one write.

Two bugs found on 2026-11-22 while registering an agent in the shared registry:
editing any agent replaced the whole [[agent]] array, which dropped every
`# ---- section` comment between agent blocks; and `new --boot` saved twice
(add_agent, then set_fields for boot/parent), so when the first save could not
land the second never ran and boot was silently lost.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from a_team import cli, config

REG = """# header comment that must survive
[settings]
default_host = "local"

# ---- Ops -----------------------------------------------------------------
[[agent]]
name = "Ops"
path = "{work}"
id = "ops"
harness = "claude"
category = "Ops"  # trailing note on a value

# ---- Clients -------------------------------------------------------------
[[agent]]
name = "Client"
path = "{work}"
id = "client"
harness = "claude"
category = "Clients"

# ---- Desks ---------------------------------------------------------------
[[desk]]
name = "Main"
agents = ["Ops"]
"""


class RegistryRewrite(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        t = Path(self._tmp.name).resolve()
        self.work = t / "work"
        self.work.mkdir()
        self.reg = t / "agents.toml"
        self.reg.write_text(REG.format(work=self.work))
        self.env = mock.patch.dict(os.environ, {
            "HOME": str(t), "A_TEAM_CONFIG": str(self.reg),
            "PATH": "/usr/bin:/bin", "A_TEAM_LOCAL_TMUX": "0",
        })
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self._tmp.cleanup()

    def assert_comments_kept(self, text: str) -> None:
        for line in ("# header comment that must survive", "# ---- Ops", "# ---- Clients",
                     "# ---- Desks", "# trailing note on a value"):
            self.assertIn(line, text)

    def test_editing_one_agent_keeps_section_comments(self) -> None:
        config.set_fields("Client", group="clients")
        text = self.reg.read_text()
        self.assert_comments_kept(text)
        self.assertEqual(tomllib.loads(text)["agent"][1]["group"], "clients")

    def test_adding_an_agent_keeps_section_comments(self) -> None:
        config.add_agent("New", str(self.work), category="Clients")
        text = self.reg.read_text()
        self.assert_comments_kept(text)
        self.assertEqual([a["name"] for a in tomllib.loads(text)["agent"]], ["Ops", "Client", "New"])

    def test_removing_an_agent_keeps_the_others_comments(self) -> None:
        config.save_agents([a for a in config.load_agents() if a["name"] != "Ops"])
        text = self.reg.read_text()
        self.assertIn("# ---- Clients", text)
        self.assertIn("# ---- Desks", text)
        self.assertEqual([a["name"] for a in tomllib.loads(text)["agent"]], ["Client"])

    def test_reordering_agents_still_writes_the_new_order(self) -> None:
        config.save_agents(list(reversed(config.load_agents())))
        names = [a["name"] for a in tomllib.loads(self.reg.read_text())["agent"]]
        self.assertEqual(names, ["Client", "Ops"])

    def test_new_with_boot_saves_once_with_boot_set(self) -> None:
        with mock.patch.object(config, "_save_raw", wraps=config._save_raw) as save:
            result = CliRunner().invoke(cli.cli, ["--no-splash", "new", "Booted", str(self.work),
                                                  "--role", "associate", "--boot"],
                                        catch_exceptions=False)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(save.call_count, 1)
        booted = next(a for a in tomllib.loads(self.reg.read_text())["agent"] if a["name"] == "Booted")
        self.assertIs(booted["boot"], True)
        self.assert_comments_kept(self.reg.read_text())


if __name__ == "__main__":
    unittest.main()
