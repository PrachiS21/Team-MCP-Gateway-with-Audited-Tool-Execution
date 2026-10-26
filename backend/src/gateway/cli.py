"""Admin CLI:  python -m gateway.cli <command> ...   (or `gateway-admin` once installed)

It works directly on the same databases as the gateway, using the same services the admin
REST API will use later. A running gateway picks up server changes within its sync
interval (GATEWAY_SYNC_INTERVAL); key revocation and user changes apply immediately.

Secrets never go on the command line (shell history, `ps`): a server credential is read
from an environment variable, from stdin, or from a hidden prompt.
"""

import argparse
import getpass
import os
import sys
from datetime import datetime
from typing import Callable, TextIO

from gateway.audit.service import AuditError, AuditLog
from gateway.auth.service import AuthError, AuthService, User
from gateway.config import Settings
from gateway.registry.crypto import SecretBox, SecretBoxError, generate_key
from gateway.registry.registry import RegistryError, ServerRegistry, safe_url
from gateway.registry.url_policy import UrlPolicyError

EXPECTED_ERRORS = (AuthError, RegistryError, UrlPolicyError, AuditError, SecretBoxError)


class CliError(Exception):
    pass


def _fmt_time(ts: datetime | None) -> str:
    return ts.astimezone().strftime("%Y-%m-%d %H:%M:%S") if ts else "-"


def _table(out: TextIO, headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        print("(none)", file=out)
        return
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    for line in [headers, *rows]:
        print("  ".join(c.ljust(widths[i]) for i, c in enumerate(line)).rstrip(), file=out)


def _default_actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:  # noqa: BLE001 - no usable user name (e.g. a bare container)
        return "cli:unknown"


def _detail(details: dict) -> str:
    """A short, safe summary of an event's details for the events table."""
    if details.get("changed"):
        return ", ".join(details["changed"])
    skip = {"email", "changed"}
    parts = [f"{k}={v}" for k, v in details.items() if k not in skip and not isinstance(v, (dict, list))]
    return ", ".join(parts) or "-"


class Cli:
    def __init__(self, settings: Settings, out: TextIO, stdin: TextIO, env: dict[str, str],
                 prompt: Callable[[str], str] = getpass.getpass, actor: str | None = None, err: TextIO | None = None):
        self.settings, self.out, self.stdin, self.env, self.prompt = settings, out, stdin, env, prompt
        self.err = err or sys.stderr
        self.actor = actor or _default_actor()
        self.auth = AuthService(settings.db_url)
        self.audit = AuditLog(settings.db_url)
        self.registry = ServerRegistry(settings.db_url, SecretBox(settings.secret_key),
                                       allow_loopback=settings.allow_loopback)

    # -- helpers ----------------------------------------------------------

    def _event(self, action: str, server_slug: str | None = None, **details) -> None:
        """Record an admin action. Never pass a secret here: only names, ids and safe values.
        The action has already succeeded, so a failure to record it is reported, not fatal."""
        try:
            self.audit.record_event(actor=self.actor, action=action, server_slug=server_slug, details=details)
        except Exception as exc:  # noqa: BLE001
            print(f"warning: the action succeeded but could not be recorded in the audit log: {exc}", file=self.err)

    def _user(self, email: str) -> User:
        user = self.auth.get_user_by_email(email)
        if user is None:
            raise CliError(f"no user with email {email!r}")
        return user

    def _read_credential(self, args) -> str | None:
        """Credential from an env var, stdin, or a hidden prompt; never from an argument."""
        if getattr(args, "credential_env", None):
            value = self.env.get(args.credential_env)
            if not value:
                raise CliError(f"environment variable {args.credential_env} is not set or empty")
            return value
        if getattr(args, "credential_stdin", False):
            return self.stdin.read().strip() or None
        if self.stdin.isatty():
            return self.prompt("Credential (input hidden): ") or None
        return None

    # -- users ------------------------------------------------------------

    def create_user(self, a) -> None:
        u = self.auth.create_user(a.email, a.role)
        print(f"created {u.role} {u.email}", file=self.out)
        self._event("user_created", email=u.email, role=u.role)

    def list_users(self, a) -> None:
        _table(self.out, ["EMAIL", "ROLE", "ACTIVE", "CREATED"],
               [[u.email, u.role, "yes" if u.active else "no", _fmt_time(u.created_at)] for u in self.auth.list_users()])

    def deactivate_user(self, a) -> None:
        self.auth.set_active(self._user(a.email).id, False)
        print(f"deactivated {a.email}: all of their keys stopped working", file=self.out)
        self._event("user_deactivated", email=a.email)

    def activate_user(self, a) -> None:
        self.auth.set_active(self._user(a.email).id, True)
        print(f"activated {a.email}", file=self.out)
        self._event("user_activated", email=a.email)

    # -- keys -------------------------------------------------------------

    def issue_key(self, a) -> None:
        issued = self.auth.issue_key(self._user(a.email).id, a.name)
        self._event("key_issued", email=a.email, key_id=issued.key_id, name=a.name)
        if a.quiet:
            print(issued.key, file=self.out)
        else:
            print(f"API key for {a.email} (id {issued.key_id}). It is shown ONCE; store it safely:\n  {issued.key}",
                  file=self.out)

    def list_keys(self, a) -> None:
        keys = self.auth.list_keys(self._user(a.email).id)
        _table(self.out, ["KEY ID", "NAME", "CREATED", "LAST USED", "STATUS"],
               [[k.key_id, k.name, _fmt_time(k.created_at), _fmt_time(k.last_used_at),
                 f"revoked {_fmt_time(k.revoked_at)}" if k.revoked else "active"] for k in keys])

    def revoke_key(self, a) -> None:
        info = self.auth.revoke_key(a.key_id)
        print(f"revoked key {a.key_id}: its next request is rejected", file=self.out)
        self._event("key_revoked", key_id=a.key_id, email=self.auth.get_user(info.user_id).email)

    # -- servers ----------------------------------------------------------

    def add_server(self, a) -> None:
        credential = None if a.credential_type == "none" else self._read_credential(a)
        if a.credential_type != "none" and not credential:
            raise CliError("this credential type needs a credential: use --credential-env NAME, "
                           "--credential-stdin, or run interactively for a hidden prompt")
        v = self.registry.create(name=a.name or a.slug, slug=a.slug, url=a.url, credential_type=a.credential_type,
                                 credential=credential, credential_header=a.credential_header,
                                 enabled=not a.disabled, timeout=a.timeout)
        print(f"added server {v.slug} ({v.url}); a running gateway picks it up within "
              f"{self.settings.sync_interval:g}s", file=self.out)
        self._event("server_created", v.slug, url=safe_url(v.url), credential_type=v.credential_type, enabled=v.enabled)

    def list_servers(self, a) -> None:
        _table(self.out, ["SLUG", "NAME", "URL", "ENABLED", "CREDENTIAL"],
               [[v.slug, v.name, v.url, "yes" if v.enabled else "no",
                 (v.credential_type + (f" ({v.credential_header})" if v.credential_header else ""))
                 if v.has_credential else "none"] for v in self.registry.list()])

    def enable_server(self, a) -> None:
        self.registry.set_enabled(self.registry.get_by_slug(a.slug).id, True)
        print(f"enabled {a.slug}", file=self.out)
        self._event("server_enabled", a.slug)

    def disable_server(self, a) -> None:
        self.registry.set_enabled(self.registry.get_by_slug(a.slug).id, False)
        print(f"disabled {a.slug}: its tools disappear within {self.settings.sync_interval:g}s", file=self.out)
        self._event("server_disabled", a.slug)

    def remove_server(self, a) -> None:
        self.registry.delete(self.registry.get_by_slug(a.slug).id)
        print(f"removed {a.slug}", file=self.out)
        self._event("server_removed", a.slug)

    def set_credential(self, a) -> None:
        view = self.registry.get_by_slug(a.slug)
        credential = self._read_credential(a)
        if not credential:
            raise CliError("no credential given: use --credential-env NAME, --credential-stdin, or the hidden prompt")
        ctype = view.credential_type if view.credential_type != "none" else "bearer"
        self.registry.update(view.id, credential_type=ctype, credential=credential)
        print(f"credential for {a.slug} replaced", file=self.out)
        self._event("server_updated", a.slug, changed=["credential"])  # that it changed, never the value

    # -- configuration changes ----------------------------------------------

    def events(self, a) -> None:
        rows = self.audit.events(server_slug=a.server, limit=a.limit)
        _table(self.out, ["TIME", "ACTOR", "ACTION", "TARGET", "DETAIL"],
               [[_fmt_time(e.ts), e.actor, e.action, e.server_slug or e.details.get("email") or e.details.get("key_id") or "-",
                 _detail(e.details)] for e in rows])

    # -- audit ------------------------------------------------------------

    def audit_log(self, a) -> None:
        user_id = self._user(a.user).id if a.user else None
        rows = self.audit.query(user_id=user_id, server_slug=a.server, outcome=a.outcome, limit=a.limit)
        _table(self.out, ["TIME", "USER", "CALL", "OUTCOME", "MS", "DETAIL"],
               [[_fmt_time(r.ts), r.user_email or "-", f"{r.server_slug}.{r.tool}" if r.server_slug else (r.tool or "-"),
                 r.outcome, str(r.duration_ms), r.error_kind or ""] for r in rows])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gateway-admin", description="Administer the MCP gateway.")
    sub = p.add_subparsers(dest="command", required=True)

    def cmd(name, help_):
        return sub.add_parser(name, help=help_)

    cmd("generate-secret-key", "print a new GATEWAY_SECRET_KEY")

    c = cmd("create-user", "create a user"); c.add_argument("email"); c.add_argument("--role", choices=["admin", "member"], default="member")
    cmd("list-users", "list users")
    c = cmd("deactivate-user", "stop all of a user's keys"); c.add_argument("email")
    c = cmd("activate-user", "re-enable a user"); c.add_argument("email")

    c = cmd("issue-key", "issue an API key (shown once)"); c.add_argument("email"); c.add_argument("--name", default="default")
    c.add_argument("--quiet", action="store_true", help="print only the key (for scripts)")
    c = cmd("list-keys", "list a user's keys (metadata only)"); c.add_argument("email")
    c = cmd("revoke-key", "revoke a key by its id"); c.add_argument("key_id")

    c = cmd("add-server", "register an upstream MCP server")
    c.add_argument("--slug", required=True, help="short unique name used as the tool prefix, e.g. woo")
    c.add_argument("--url", required=True); c.add_argument("--name")
    c.add_argument("--credential-type", choices=["none", "bearer", "header"], default="none")
    c.add_argument("--credential-header", help="header name when --credential-type header")
    c.add_argument("--credential-env", metavar="VAR", help="read the credential from this environment variable")
    c.add_argument("--credential-stdin", action="store_true", help="read the credential from stdin")
    c.add_argument("--timeout", type=float, default=10.0); c.add_argument("--disabled", action="store_true")
    cmd("list-servers", "list registered servers")
    for name, h in (("enable-server", "enable a server"), ("disable-server", "disable a server"),
                    ("remove-server", "delete a server")):
        c = cmd(name, h); c.add_argument("slug")
    c = cmd("set-credential", "replace a server's credential"); c.add_argument("slug")
    c.add_argument("--credential-env", metavar="VAR"); c.add_argument("--credential-stdin", action="store_true")

    c = cmd("events", "show configuration changes (e.g. servers synced from the seed file), newest first")
    c.add_argument("--server", metavar="SLUG"); c.add_argument("--limit", type=int, default=20)

    c = cmd("audit", "show the audit log, newest first")
    c.add_argument("--user", metavar="EMAIL"); c.add_argument("--server", metavar="SLUG")
    c.add_argument("--outcome"); c.add_argument("--limit", type=int, default=20)
    return p


def main(argv: list[str] | None = None, *, settings: Settings | None = None, out: TextIO | None = None,
         err: TextIO | None = None, stdin: TextIO | None = None, env: dict[str, str] | None = None,
         prompt: Callable[[str], str] = getpass.getpass, actor: str | None = None) -> int:
    out, err, stdin, env = out or sys.stdout, err or sys.stderr, stdin or sys.stdin, env if env is not None else dict(os.environ)
    args = build_parser().parse_args(argv)
    if args.command == "generate-secret-key":
        print(generate_key(), file=out)
        return 0
    try:
        cli = Cli(settings or Settings.from_env(), out, stdin, env, prompt, actor, err)
        getattr(cli, args.command.replace("-", "_") if args.command != "audit" else "audit_log")(args)
    except (CliError, *EXPECTED_ERRORS) as exc:
        print(f"error: {exc}", file=err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
