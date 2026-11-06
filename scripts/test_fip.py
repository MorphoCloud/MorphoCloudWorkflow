"""fip.py (AUTOMATION_AUDIT_2026-11.md, class 1) against a fake Neutron that
enforces revision numbers the way Jetstream2's `revision-if-match` does.
Run: python -m pytest scripts/test_fip.py"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "fip",
    Path(__file__).resolve().parent.parent / ".github" / "actions" / "fip" / "fip.py",
)
fip = importlib.util.module_from_spec(spec)
sys.modules["fip"] = fip
spec.loader.exec_module(fip)


class HttpError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class FakeNeutron:
    """Floating IPs with revisions; servers with one port each. `hooks` run
    before a given call, so a test can make another actor act in between."""

    def __init__(self, addresses=(), pool=(), servers=()):
        self.fips = {}
        self.pool = list(pool)  # what create_ip hands out next
        self._ports = {}
        self.servers = {}
        self.hooks = {}
        self.calls = []
        self.spurious_500 = set()  # ids whose next update answers 500 but applies
        for name in servers:
            self.add_server(name)
        for a in addresses:
            self.add_fip(a)

    # --- setup helpers
    def add_server(self, name):
        sid = f"srv-{name}"
        self.servers[name] = SimpleNamespace(id=sid, name=name)
        self._ports[sid] = SimpleNamespace(
            id=f"port-{name}", device_owner="compute:nova"
        )

    def port_of(self, name):
        return self._ports[self.servers[name].id].id

    def add_fip(self, address, port=None):
        fid = f"id-{address}"
        self.fips[fid] = {
            "id": fid,
            "floating_ip_address": address,
            "port_id": port,
            "rev": 1,
        }
        return fid

    def view(self, fid):
        f = self.fips[fid]
        return SimpleNamespace(
            id=f["id"],
            floating_ip_address=f["floating_ip_address"],
            port_id=f["port_id"],
            revision_number=f["rev"],
        )

    def _hook(self, name):
        self.calls.append(name)
        for fn in self.hooks.pop(name, []):
            fn()

    # --- the sdk surface fip.py uses
    @property
    def compute(self):
        return self

    @property
    def network(self):
        return self

    def find_server(self, name, **_kw):
        return self.servers.get(name)

    def ports(self, device_id):
        p = self._ports.get(device_id)
        return [p] if p else []

    def ips(self, port_id=None, **_q):
        self._hook("ips")
        out = [self.view(fid) for fid in sorted(self.fips)]
        if port_id is not None:
            out = [ip for ip in out if ip.port_id == port_id]
        return out

    def get_ip(self, fid):
        if fid not in self.fips:
            raise HttpError(404)
        return self.view(fid)

    def update_ip(self, ip, if_revision=None, **attrs):
        self._hook("update_ip")
        f = self.fips.get(ip.id)
        if f is None:
            raise HttpError(404)
        if if_revision is not None and if_revision != f["rev"]:
            raise HttpError(412)
        f["port_id"] = attrs["port_id"]
        f["rev"] += 1
        if ip.id in self.spurious_500:
            self.spurious_500.discard(ip.id)
            raise HttpError(500)  # applied, but Jetstream2 says otherwise

    def delete_ip(self, ip, ignore_missing=True, if_revision=None):
        f = self.fips.get(ip.id)
        if f is None:
            if ignore_missing:
                return
            raise HttpError(404)
        if if_revision is not None and if_revision != f["rev"]:
            raise HttpError(412)
        del self.fips[ip.id]

    def create_ip(self, **_kw):
        self._hook("create_ip")
        if not self.pool:
            raise HttpError(409)  # no more addresses
        fid = self.add_fip(self.pool.pop(0))
        return self.view(fid)

    def find_network(self, *_a, **_kw):
        return SimpleNamespace(id="net-public")


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(fip, "log", lambda _m: None)
    monkeypatch.setattr(fip.time, "sleep", lambda _s: None)
    fip._EXTERNAL.clear()


def no_sleep(_s):
    return None


def test_attach_takes_a_spare_and_reads_it_back():
    n = FakeNeutron(addresses=["149.165.1.1"], servers=["a"])
    ip = fip.attach(n, "a", sleep=no_sleep)
    assert ip.floating_ip_address == "149.165.1.1"
    assert n.fips["id-149.165.1.1"]["port_id"] == n.port_of("a")


def test_attach_is_idempotent():
    n = FakeNeutron(servers=["a"])
    n.add_fip("149.165.1.1", port=n.port_of("a"))
    n.add_fip("149.165.1.2")
    assert fip.attach(n, "a", sleep=no_sleep).floating_ip_address == "149.165.1.1"
    assert n.fips["id-149.165.1.2"]["port_id"] is None


def test_attach_prefers_the_old_address():
    n = FakeNeutron(addresses=["149.165.1.1", "149.165.1.2"], servers=["a"])
    ip = fip.attach(n, "a", prefer="149.165.1.2", sleep=no_sleep)
    assert ip.floating_ip_address == "149.165.1.2"


def test_two_takers_on_one_spare_never_share_it_1a_1b():
    # Audit 1a/1b: a create on one runner and an unshelve on the other read
    # the same single spare. The other actor attaches between our read and
    # our write; our compare-and-swap loses (412) and we mint our own.
    n = FakeNeutron(
        addresses=["149.165.1.1"], pool=["149.165.2.2"], servers=["create", "unshelve"]
    )

    def other_takes_it():
        n.fips["id-149.165.1.1"]["port_id"] = n.port_of("unshelve")
        n.fips["id-149.165.1.1"]["rev"] += 1

    n.hooks["update_ip"] = [other_takes_it]
    ip = fip.attach(n, "create", sleep=no_sleep)
    assert ip.floating_ip_address == "149.165.2.2"
    assert n.fips["id-149.165.1.1"]["port_id"] == n.port_of("unshelve")
    assert n.fips["id-149.165.2.2"]["port_id"] == n.port_of("create")


def test_an_address_on_another_port_is_never_taken_as_ours_1c():
    # Audit 1c: the old check said "the address has a port, so it is ours".
    # The only address in sight is attached to someone else: we must not
    # report it, we mint our own.
    n = FakeNeutron(pool=["149.165.2.2"], servers=["mine", "theirs"])
    n.add_fip("149.165.1.1", port=n.port_of("theirs"))
    ip = fip.attach(n, "mine", sleep=no_sleep)
    assert ip.floating_ip_address == "149.165.2.2"
    assert n.fips["id-149.165.1.1"]["port_id"] == n.port_of("theirs")


def test_a_spurious_500_that_applied_counts_as_attached():
    n = FakeNeutron(addresses=["149.165.1.1"], servers=["a"])
    n.spurious_500.add("id-149.165.1.1")
    assert fip.attach(n, "a", sleep=no_sleep).floating_ip_address == "149.165.1.1"


def test_policing_deletes_the_spare_mid_attach_1e():
    # Audit 1e: the spare is deleted between our read and our write (404):
    # we move on and mint, and never report a vanished address.
    n = FakeNeutron(addresses=["149.165.1.1"], pool=["149.165.2.2"], servers=["a"])
    n.hooks["update_ip"] = [lambda: n.fips.pop("id-149.165.1.1")]
    assert fip.attach(n, "a", sleep=no_sleep).floating_ip_address == "149.165.2.2"


def test_a_dry_pool_is_reported_not_retried_forever():
    n = FakeNeutron(servers=["a"])
    with pytest.raises(fip.NoAddressError):
        fip.attach(n, "a", sleep=no_sleep)


def test_a_non_routable_mint_is_deleted_and_never_attached():
    n = FakeNeutron(pool=["172.17.21.1", "172.17.21.2"], servers=["a"])
    with pytest.raises(fip.NoAddressError):
        fip.attach(n, "a", sleep=no_sleep)
    assert n.fips == {}


def test_never_mints_in_a_loop_when_attaches_keep_failing():
    # Something other than the pool refuses (a 409 on every attach): at most
    # MAX_MINTS_PER_ATTACH new addresses, then a clear failure.
    n = FakeNeutron(pool=[f"149.165.2.{i}" for i in range(10)], servers=["a"])

    def refuse(*_a, **_kw):
        raise HttpError(409)

    n.update_ip = refuse
    with pytest.raises(RuntimeError):
        fip.attach(n, "a", sleep=no_sleep)
    assert len(n.fips) == fip.MAX_MINTS_PER_ATTACH


def test_a_dead_address_on_the_port_is_released_first():
    n = FakeNeutron(addresses=["149.165.1.1"], servers=["a"])
    n.add_fip("172.17.21.9", port=n.port_of("a"))
    assert fip.attach(n, "a", sleep=no_sleep).floating_ip_address == "149.165.1.1"
    assert n.fips["id-172.17.21.9"]["port_id"] is None


def test_detach_touches_only_this_servers_port_1f_1i():
    # Audit 1f/1i: the old shelve removed an address by name, from wherever
    # it was bound. Detach names no address: another server's stays put.
    n = FakeNeutron(servers=["shelving", "other"])
    n.add_fip("149.165.1.1", port=n.port_of("shelving"))
    n.add_fip("149.165.1.2", port=n.port_of("other"))
    assert fip.detach(n, "shelving", sleep=no_sleep) == ["149.165.1.1"]
    assert n.fips["id-149.165.1.1"]["port_id"] is None
    assert n.fips["id-149.165.1.2"]["port_id"] == n.port_of("other")


def test_detach_after_someone_else_moved_it_does_nothing_1f():
    # Policing released our address, and another server attached it, between
    # our listing and our detach: the address is no longer ours to touch.
    n = FakeNeutron(servers=["shelving", "other"])
    n.add_fip("149.165.1.1", port=n.port_of("shelving"))

    def moved():
        n.fips["id-149.165.1.1"]["port_id"] = n.port_of("other")
        n.fips["id-149.165.1.1"]["rev"] += 2

    original_get = n.get_ip

    def get_after_move(fid):
        moved()
        n.get_ip = original_get
        return original_get(fid)

    n.get_ip = get_after_move
    assert fip.detach(n, "shelving", sleep=no_sleep) == []
    assert n.fips["id-149.165.1.1"]["port_id"] == n.port_of("other")


def test_detach_of_a_gone_server_is_a_no_op():
    n = FakeNeutron()
    assert fip.detach(n, "gone", sleep=no_sleep) == []


def test_attach_to_a_missing_server_says_so():
    n = FakeNeutron(addresses=["149.165.1.1"])
    with pytest.raises(fip.NoServerError):
        fip.attach(n, "missing", sleep=no_sleep)


def test_ensure_spare_mints_only_when_none_is_free():
    n = FakeNeutron(addresses=["149.165.1.1"], pool=["149.165.2.2"])
    fip.ensure_spare(n)
    assert "create_ip" not in n.calls
    n2 = FakeNeutron(pool=["149.165.2.2"])
    fip.ensure_spare(n2)
    assert n2.fips["id-149.165.2.2"]["port_id"] is None
    with pytest.raises(fip.NoAddressError):
        fip.ensure_spare(FakeNeutron())


def test_main_prints_outputs_and_exit_codes(monkeypatch, capsys):
    n = FakeNeutron(addresses=["149.165.1.1"], pool=["149.165.2.2"], servers=["a"])
    monkeypatch.setattr(fip, "connect", lambda: n)
    monkeypatch.setenv("OS_CLOUD", "test")
    assert fip.main(["attach", "--server", "a"]) == 0
    out = capsys.readouterr().out.split()
    assert out == ["floating_ip_address=149.165.1.1", "floating_ip_uuid=id-149.165.1.1"]
    assert n.fips["id-149.165.2.2"]["port_id"] is None  # one kept in hand
    monkeypatch.setattr(fip, "connect", lambda: FakeNeutron(servers=["b"]))
    assert fip.main(["attach", "--server", "b"]) == fip.EXIT_NO_ADDRESS
    assert fip.main(["attach", "--server", "nope"]) == fip.EXIT_NO_SERVER
