"""Offline fixtures only: no production paths, network TCP, or configfs access."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest

import mic_lease as m


OWNER_A = "a" * 32
OWNER_B = "b" * 32
EPOCH_A = "1" * 32
EPOCH_B = "2" * 32


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeJack:
    def __init__(self, value=1):
        self.value = value
        self.writes = []
        self.read_error = False
        self.write_error = False
        self.mismatch = False
        self.detect = True

    def ready(self):
        if not self.detect:
            raise OSError("detect disabled")
        self.read()

    def read(self):
        if self.read_error:
            raise OSError("fixture read failure")
        return self.value

    def write(self, value):
        self.writes.append(value)
        if self.write_error:
            raise OSError("fixture write failure")
        self.value = value
        if self.mismatch:
            raise OSError("fixture readback failure")
        return self.read()


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.jack = FakeJack()
        self.controller = m.LeaseController(self.jack, clock=self.clock, boot_epoch=EPOCH_A)

    def request(self, action, owner=OWNER_A, seq=1, epoch=EPOCH_A):
        req = {"action": action}
        if action != "status":
            req.update(owner=owner, seq=seq, bootEpoch=epoch)
        return self.controller.handle(req)

    def active(self, owner=OWNER_A, seq=1):
        self.assertTrue(self.request("claim", owner, seq)["ok"])
        self.assertTrue(self.request("commit", owner, seq)["ok"])

    def test_startup_forces_off(self):
        self.assertEqual(self.jack.writes, [0])
        self.assertEqual(self.request("status")["phase"], "idle")

    def test_claim_is_reserved_off_and_commit_enables(self):
        result = self.request("claim")
        self.assertEqual((result["phase"], result["jack"], result["ttlMs"]), ("reserved", 0, 10000))
        result = self.request("commit")
        self.assertEqual((result["phase"], result["jack"], result["code"]), ("active", 1, "committed"))

    def test_commit_without_claim_never_opens(self):
        self.assertEqual(self.request("commit")["code"], "not_owner")
        self.assertEqual(self.jack.writes, [0])

    def test_renew_without_claim_never_opens(self):
        self.assertEqual(self.request("renew")["code"], "not_owner")
        self.assertEqual(self.jack.writes, [0])

    def test_reserved_renew_does_not_write(self):
        self.request("claim")
        previous = list(self.jack.writes)
        self.clock.advance(3)
        result = self.request("renew")
        self.assertEqual((result["phase"], result["jack"], result["ttlMs"]), ("reserved", 0, 10000))
        self.assertEqual(self.jack.writes, previous)

    def test_active_renew_does_not_write(self):
        self.active()
        previous = list(self.jack.writes)
        self.clock.advance(3)
        self.assertEqual(self.request("renew")["ttlMs"], 10000)
        self.assertEqual(self.jack.writes, previous)

    def test_second_owner_and_same_owner_new_seq_cannot_steal(self):
        self.active()
        before = list(self.jack.writes)
        self.assertEqual(self.request("claim", OWNER_B)["code"], "busy")
        self.assertEqual(self.request("claim", OWNER_A, 2)["code"], "busy")
        self.assertEqual(self.request("commit", OWNER_B)["code"], "not_owner")
        self.assertEqual(self.jack.writes, before)
        self.assertEqual(self.controller.lease.owner, OWNER_A)

    def test_release_before_claim_and_commit_tombstones(self):
        self.assertTrue(self.request("release")["ok"])
        for action in ("claim", "commit", "renew"):
            self.assertEqual(self.request(action)["code"], "stale")
        self.assertEqual(self.jack.writes, [0])
        self.assertTrue(self.request("claim", seq=2)["ok"])

    def test_release_reserved_blocks_late_commit(self):
        self.request("claim")
        self.request("release")
        self.assertEqual(self.request("commit")["code"], "stale")
        self.assertEqual(self.jack.value, 0)

    def test_old_release_cannot_stop_new_activation(self):
        self.active(seq=1)
        self.request("release", seq=1)
        self.active(seq=2)
        before = list(self.jack.writes)
        self.assertTrue(self.request("release", seq=1)["ok"])
        self.assertEqual(self.jack.writes, before)
        self.assertEqual(self.controller.lease.seq, 2)
        self.assertEqual(self.jack.value, 1)

    def test_foreign_release_does_not_stop_owner(self):
        self.active()
        before = list(self.jack.writes)
        self.assertTrue(self.request("release", OWNER_B, 100)["ok"])
        self.assertEqual(self.jack.writes, before)
        self.assertEqual(self.controller.lease.owner, OWNER_A)

    def test_release_higher_seq_closes_lower_and_prevents_replay(self):
        self.active(seq=3)
        self.request("release", seq=5)
        for seq in (1, 3, 5):
            self.assertEqual(self.request("claim", seq=seq)["code"], "stale")
        self.assertTrue(self.request("claim", seq=6)["ok"])

    def test_duplicate_claim_does_not_downgrade_or_extend(self):
        self.active()
        before = list(self.jack.writes)
        self.clock.advance(2)
        result = self.request("claim")
        self.assertEqual((result["phase"], result["jack"], result["ttlMs"]), ("active", 1, 8000))
        self.assertEqual(self.jack.writes, before)

    def test_duplicate_commit_does_not_write_again(self):
        self.active()
        before = list(self.jack.writes)
        self.assertTrue(self.request("commit")["ok"])
        self.assertEqual(self.jack.writes, before)

    def test_expiry_off_and_tombstone_active(self):
        self.active()
        self.clock.advance(10)
        self.controller.tick()
        self.assertIsNone(self.controller.lease)
        self.assertEqual(self.jack.value, 0)
        self.assertEqual(self.request("claim")["code"], "stale")
        self.assertEqual(self.request("commit")["code"], "stale")

    def test_expiry_off_and_tombstone_reserved(self):
        self.request("claim")
        self.clock.advance(10)
        self.assertEqual(self.request("commit")["code"], "stale")
        self.assertEqual(self.jack.value, 0)

    def test_no_lease_status_does_not_force_off_manual_state(self):
        self.jack.value = 1
        before = list(self.jack.writes)
        result = self.request("status")
        self.assertEqual((result["phase"], result["jack"]), ("idle", 1))
        self.assertEqual(self.jack.writes, before)

    def test_external_state_change_while_active_is_fault_not_reopen(self):
        self.active()
        self.jack.value = 0
        result = self.request("renew")
        self.assertEqual((result["ok"], result["code"], result["jack"]), (False, "hardware_error", None))
        self.assertEqual(self.jack.value, 0)
        self.assertIsNone(self.controller.lease)

    def test_detection_removed_after_claim_blocks_commit(self):
        self.request("claim")
        self.jack.detect = False
        result = self.request("commit")
        self.assertEqual(result["code"], "hardware_error")
        self.assertEqual(self.jack.value, 0)
        self.assertNotIn(1, self.jack.writes)

    def test_detection_removed_while_active_renew_faults_off(self):
        self.active()
        self.jack.detect = False
        result = self.request("renew")
        self.assertEqual((result["phase"], result["jack"]), ("fault", None))
        self.assertEqual(self.jack.value, 0)

    def test_detection_removed_while_active_status_faults_off(self):
        self.active()
        self.jack.detect = False
        self.assertEqual(self.request("status")["code"], "hardware_error")
        self.assertEqual(self.jack.value, 0)

    def test_restart_changes_epoch_forces_off_rejects_old_requests(self):
        self.active()
        self.controller = m.LeaseController(self.jack, clock=self.clock, boot_epoch=EPOCH_B)
        self.assertEqual(self.jack.value, 0)
        self.request("claim", OWNER_B, 1, EPOCH_B)
        self.request("commit", OWNER_B, 1, EPOCH_B)
        before = list(self.jack.writes)
        for action in ("claim", "commit", "renew", "release"):
            self.assertEqual(self.request(action)["code"], "epoch_mismatch")
        self.assertEqual(self.jack.writes, before)
        self.assertEqual(self.controller.lease.owner, OWNER_B)

    def test_cap_refuses_new_owners_without_evicting(self):
        self.controller.max_owners = 2
        self.request("release", OWNER_A)
        self.request("release", OWNER_B)
        for action in ("release", "claim"):
            self.assertEqual(self.request(action, "c" * 32)["code"], "capacity")
        self.assertEqual(self.request("claim", OWNER_A)["code"], "stale")
        self.assertTrue(self.request("claim", OWNER_A, 2)["ok"])
        self.assertEqual(len(self.controller.closed), 2)

    def test_invalid_fields_and_values_never_write(self):
        before = list(self.jack.writes)
        good = {"action": "claim", "owner": OWNER_A, "seq": 1, "bootEpoch": EPOCH_A}
        invalid = [None, [], {}, {"action": "shell"}, {"action": "status", "owner": OWNER_A}]
        for field, values in {"owner": ["a", "A" * 32, "a;id", 1],
                              "seq": [True, 0, -1, 1.0, "1", m.MAX_SEQ + 1],
                              "bootEpoch": [None, "2", "F" * 32]}.items():
            for value in values:
                invalid.append(dict(good, **{field: value}))
        invalid.append(dict(good, extra="ignored?"))
        for request in invalid:
            with self.subTest(request=request):
                self.assertEqual(self.controller.handle(request)["code"], "invalid_request")
        self.assertEqual(self.jack.writes, before)

    def test_maximum_safe_sequence(self):
        self.assertTrue(self.request("claim", seq=m.MAX_SEQ)["ok"])
        self.assertTrue(self.request("release", seq=m.MAX_SEQ)["ok"])
        self.assertEqual(self.request("claim", seq=m.MAX_SEQ)["code"], "stale")

    def test_write_failure_faults_and_tombstones(self):
        self.request("claim")
        self.jack.write_error = True
        result = self.request("commit")
        self.assertEqual((result["ok"], result["phase"], result["jack"]), (False, "fault", None))
        self.assertEqual(self.controller.closed[OWNER_A], 1)

    def test_readback_failure_never_reports_committed(self):
        self.request("claim")
        self.jack.mismatch = True
        result = self.request("commit")
        self.assertFalse(result["ok"])
        self.assertEqual(self.jack.value, 0)

    def test_status_read_failure_has_no_false_ack(self):
        self.jack.read_error = True
        result = self.request("status")
        self.assertFalse(result["ok"])
        self.assertIsNone(result["jack"])
        self.assertEqual(self.jack.writes, [0])

    def test_graceful_shutdown_is_off(self):
        self.active()
        self.controller.shutdown()
        self.assertEqual(self.jack.value, 0)
        self.assertIsNone(self.controller.lease)

    def test_status_never_discloses_owner(self):
        self.active()
        self.assertNotIn(OWNER_A, json.dumps(self.request("status")))

    def test_concurrent_claims_grant_only_one(self):
        barrier = threading.Barrier(8)
        results = []
        def claim(number):
            barrier.wait()
            results.append(self.request("claim", f"{number:032x}"))
        threads = [threading.Thread(target=claim, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(result["ok"] for result in results), 1)
        self.assertEqual(sum(result["code"] == "busy" for result in results), 7)


class ProtocolAndFixtureTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "fcntl singleton fixture needs Linux")
    def test_stop_post_does_not_close_another_live_daemon(self):
        with tempfile.TemporaryDirectory() as temp:
            lock = Path(temp) / "daemon.lock"
            jack = FakeJack(1)
            fd = m.acquire_singleton_lock(lock)
            try:
                self.assertFalse(m.failsafe_off(jack, lock))
                self.assertEqual((jack.value, jack.writes), (1, []))
                with self.assertRaises(BlockingIOError):
                    m.acquire_singleton_lock(lock)
            finally:
                os.close(fd)
            self.assertTrue(m.failsafe_off(jack, lock))
            self.assertEqual((jack.value, jack.writes), (0, [0]))

    @unittest.skipUnless(os.name == "posix", "fcntl singleton fixture needs Linux")
    def test_stop_post_rejects_symlink_lock_without_jack_write(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "target").touch()
            (root / "daemon.lock").symlink_to(root / "target")
            jack = FakeJack(1)
            with self.assertRaises(OSError):
                m.failsafe_off(jack, root / "daemon.lock")
            self.assertEqual(jack.writes, [])

    def test_cli_whitelist_and_argument_order(self):
        self.assertEqual(m.parse_client_args(["status"]), {"action": "status"})
        result = m.parse_client_args(["claim", OWNER_A, "42", EPOCH_A])
        self.assertEqual(result, {"action": "claim", "owner": OWNER_A, "seq": 42, "bootEpoch": EPOCH_A})
        for args in (["on"], ["claim", OWNER_A, "01", EPOCH_A],
                     ["claim", OWNER_A, "1;id", EPOCH_A], ["status", "--path", "/tmp/x"],
                     ["claim", OWNER_A, str(m.MAX_SEQ + 1), EPOCH_A]):
            with self.assertRaises(m.ProtocolError):
                m.parse_client_args(args)

    def test_strict_json_framing(self):
        self.assertEqual(m.decode_request(b'{"action":"status"}\n'), {"action": "status"})
        for payload in (b'{"action":"status"}', b'{"action":"status","action":"claim"}\n',
                        b'{"action":"status"}\n{}\n', b'{"seq":NaN}\n', b'\xff\n', b'x' * 2049 + b'\n'):
            with self.assertRaises(m.ProtocolError):
                m.decode_request(payload)

    def test_existing_fixture_files_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            connected, detect = root / "connected", root / "detect"
            connected.write_text("1\n", encoding="ascii")
            detect.write_text("1\n", encoding="ascii")
            backend = m.JackBackend(connected, detect)
            backend.ready()
            self.assertEqual(backend.write(0), 0)
            self.assertEqual(backend.write(1), 1)
            connected.unlink()
            with self.assertRaises(OSError):
                backend.write(0)
            self.assertFalse(connected.exists())

    def test_missing_detection_refuses_startup(self):
        jack = FakeJack()
        jack.detect = False
        with self.assertRaises(OSError):
            m.LeaseController(jack)
        self.assertEqual(jack.writes, [])

    @unittest.skipUnless(os.name == "posix", "fixture symlink guard tested on Linux")
    def test_binding_guards_reject_unbound_unlinked_wrong_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            function = root / "function"
            function.mkdir()
            connected, detect = function / "connected", function / "detect"
            connected.write_text("1\n", encoding="ascii")
            detect.write_text("1\n", encoding="ascii")
            udc, link = root / "UDC", root / "config-link"
            udc.write_text("controller-name\n", encoding="ascii")
            link.symlink_to(function, target_is_directory=True)
            backend = m.JackBackend(connected, detect, udc=udc, config_link=link)
            controller = m.LeaseController(backend, boot_epoch=EPOCH_A)
            request = {"action": "claim", "owner": OWNER_A, "seq": 1, "bootEpoch": EPOCH_A}
            self.assertTrue(controller.handle(request)["ok"])
            request["action"] = "commit"
            self.assertTrue(controller.handle(request)["ok"])
            udc.write_text("\n", encoding="ascii")
            self.assertEqual(controller.handle({"action": "status"})["code"], "hardware_error")
            self.assertEqual(connected.read_text(encoding="ascii"), "0\n")
            udc.write_text("controller-name\n", encoding="ascii")
            link.unlink()
            with self.assertRaises(OSError):
                backend.ready()
            wrong = root / "wrong-function"
            wrong.mkdir()
            link.symlink_to(wrong, target_is_directory=True)
            with self.assertRaises(OSError):
                backend.ready()

    def test_client_invalid_args_returns_one_json_line_and_exit_zero(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = m.main(["client", "--path", "fake"])
        self.assertEqual(code, 0)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertEqual(json.loads(output.getvalue())["code"], "invalid_request")

    def test_unavailable_socket_returns_structured_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            result = m.call_socket(Path(temp) / "absent.sock", {"action": "status"})
            self.assertEqual(result["code"], "unavailable")
            self.assertIsNone(result["jack"])

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "AF_UNIX not available")
    def test_unix_socket_roundtrip_and_watchdog(self):
        with tempfile.TemporaryDirectory() as temp:
            socket_path = Path(temp) / "lease.sock"
            jack, clock = FakeJack(), Clock()
            controller = m.LeaseController(jack, clock=clock, boot_epoch=EPOCH_A)
            server = m.LeaseServer(socket_path, controller)
            stop = threading.Event()
            thread = threading.Thread(target=m.run_server, args=(server, stop), daemon=True)
            thread.start()
            try:
                status = m.call_socket(socket_path, {"action": "status"})
                self.assertEqual(status["bootEpoch"], EPOCH_A)
                request = {"action": "claim", "owner": OWNER_A, "seq": 1, "bootEpoch": EPOCH_A}
                self.assertEqual(m.call_socket(socket_path, request)["phase"], "reserved")
                request["action"] = "commit"
                self.assertEqual(m.call_socket(socket_path, request)["jack"], 1)
                clock.advance(11)
                deadline = time.monotonic() + 1.0
                while jack.value and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(jack.value, 0)
                self.assertEqual(m.call_socket(socket_path, request)["code"], "stale")
                self.assertEqual(m.call_socket(socket_path, {"action": "arbitrary"})["code"], "invalid_request")
            finally:
                stop.set()
                thread.join(timeout=2)
                server.server_close()
                controller.shutdown()
                self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main(verbosity=2)
