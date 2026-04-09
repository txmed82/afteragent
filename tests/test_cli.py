import unittest

from afteragent.cli import normalize_replay_args


class CliTests(unittest.TestCase):
    def test_normalize_replay_args_extracts_misplaced_options(self) -> None:
        summary, apply_interventions, no_stream, command = normalize_replay_args(
            None,
            False,
            False,
            [
                "--summary",
                "live replay",
                "--apply-interventions",
                "--no-stream",
                "--",
                "python3",
                "-c",
                "print('ok')",
            ],
        )

        self.assertEqual(summary, "live replay")
        self.assertTrue(apply_interventions)
        self.assertTrue(no_stream)
        self.assertEqual(command, ["python3", "-c", "print('ok')"])


if __name__ == "__main__":
    unittest.main()
