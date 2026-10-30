#!/usr/bin/env bash
# Initial deployment only. Uses the existing personal launcher, not a new runtime.
set -euo pipefail
umask 077
make_software_readable() {
    # Only immutable software trees; never traverse links into private data.
    find -P "$@" -type d -exec chmod a+rx,go-w {} +
    find -P "$@" -type f -exec chmod a+rX,go-w {} +
}
MODE="${1:-}"
BUNDLE="${2:-}"
OWNER="${3:-owner}"
if [[ "$MODE" != "--check" && "$MODE" != "--install" ]] || [[ -z "$BUNDLE" ]]; then
    echo "用法：bootstrap.sh --check|--install <私有部署包目录> [实例用户名]" >&2
    exit 2
fi
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# No installs, credentials or server imports in this input-only check.
META="$(python3 -I - "$BUNDLE" "$OWNER" <<'PY'
import hashlib, ipaddress, pathlib, re, sys, tarfile, urllib.parse
def require(condition):
    if not condition:
        raise ValueError("invalid deployment input")
try:
    root = pathlib.Path(sys.argv[1]).resolve(strict=True)
    owner = sys.argv[2]
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", owner))
    def text(name):
        path = root / name
        require(not path.is_symlink() and path.is_file() and path.stat().st_size < 32768)
        return path.read_text().strip()
    revision, origin = text("SOURCE_REVISION"), text("PUBLIC_ORIGIN")
    require(re.fullmatch(r"[0-9a-f]{40}", revision))
    require(not any(ord(c) <= 32 or ord(c) == 127 for c in origin))
    url = urllib.parse.urlsplit(origin)
    host = url.hostname or ""
    require(origin == "https://" + host and not url.username and not url.query)
    # This initial-host package supports IPv4 or ordinary DNS, not a custom port.
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        require(len(host) <= 253 and all(re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label
        ) for label in host.split(".")))
    else:
        require(address.version == 4)
    expected = {}
    for line in text("SHA256SUMS").splitlines():
        digest, name = line.split()
        require(re.fullmatch(r"[0-9a-f]{64}", digest) and name not in expected)
        expected[name] = digest
    require(set(expected) == {"app.tar.gz", "codex.tar.gz", "caddy.tar.gz"})
    for name, digest in expected.items():
        path = root / name
        require(path.is_file() and not path.is_symlink())
        with path.open("rb") as stream:
            require(hashlib.file_digest(stream, "sha256").hexdigest() == digest)
        with tarfile.open(path, "r:gz") as bundle:
            names = []
            for entry in bundle.getmembers():
                p = pathlib.PurePosixPath(entry.name)
                require(not p.is_absolute() and ".." not in p.parts)
                require(entry.isfile() or entry.isdir())
                if entry.isfile():
                    names.append(entry.name)
            if name == "app.tar.gz":
                require(len({n.split("/")[0] for n in names}) == 1)
                require(any(n.endswith("/meyo.py") for n in names))
                require(any(n.endswith("/release/legion-service/requirements-core.txt") for n in names))
            elif name == "codex.tar.gz":
                require("codex-x86_64-unknown-linux-musl" in names)
            else:
                require("caddy" in names)
    print("\n".join((str(root), revision, origin, host, owner)))
except (OSError, ValueError, tarfile.TarError, UnicodeError):
    print("部署输入不可用：核对版本、来源、校验和及压缩包路径；未执行安装。", file=sys.stderr)
    sys.exit(2)
PY
)"
if [[ "$MODE" == "--check" ]]; then
    echo "部署输入校验通过；未安装、未启动服务、未读取模型凭据。"
    exit 0
fi
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 || "$EUID" != 0 ]]; then
    echo "安装仅适用于已授权的 Linux x86_64 主机，须以 root 执行。" >&2
    exit 2
fi
mapfile -t VALUES <<< "$META"
BUNDLE="${VALUES[0]}"; REVISION="${VALUES[1]}"
ORIGIN="${VALUES[2]}"; PUBLIC_HOST="${VALUES[3]}"; OWNER="${VALUES[4]}"
ROOT=/opt/meyo-legion
STATE=/var/lib/meyo-legion
for service in meyo-legion.service meyo-proxy.service; do
    status="$(systemctl show "$service" -p ActiveState --value)"
    if [[ "$status" != inactive ]] ||
       [[ "$(systemctl show "$service" -p MainPID --value)" != 0 ]] ||
       [[ "$(systemctl show "$service" -p ControlPID --value)" != 0 ]]; then
        echo "已有服务正在运行：$service。先核实活任务；本脚本不会停机或覆盖。" >&2
        exit 2
    fi
done
[[ ! -L "$STATE" ]] || { echo "实例目录不能是符号链接。" >&2; exit 2; }
if [[ -e "$ROOT/current" || -L "$ROOT/current" ]]; then
    [[ "$(readlink -f "$ROOT/current")" == "$ROOT/releases/$REVISION" ]] ||
        { echo "已有其他版本，改走受控升级流程；没有切换。" >&2; exit 2; }
fi
for user in meyo-legion meyo-proxy; do
    home="/var/lib/$user"
    [[ "$user" != meyo-legion ]] || home="$STATE/home"
    if id "$user" >/dev/null 2>&1; then
        [[ "$(id -u "$user")" != 0 &&
           "$(getent passwd "$user" | cut -d: -f6)" == "$home" ]] ||
            { echo "已有同名账号归属不同，未修改。" >&2; exit 2; }
    fi
done
if [[ -d "$STATE" ]]; then
    [[ "$(stat -c %U "$STATE")" == meyo-legion ]] ||
        { echo "已有数据归属不同，未更换 owner。" >&2; exit 2; }
fi

exec 9>/var/lock/meyo-runtime-install.lock
flock -n 9 || { echo "另一个军团安装正在执行。" >&2; exit 2; }
export DEBIAN_FRONTEND=noninteractive
timeout 600 apt-get update
timeout 900 apt-get install -y python3.12-venv ca-certificates curl git procps ripgrep bubblewrap apparmor restic
for user in meyo-legion meyo-proxy; do
    home="/var/lib/$user"
    [[ "$user" != meyo-legion ]] || home="$STATE/home"
    if id "$user" >/dev/null 2>&1; then
        [[ "$(getent passwd "$user" | cut -d: -f6)" == "$home" ]] ||
            { echo "已有同名账号的 HOME 不同，未重用。" >&2; exit 2; }
    else
        useradd --system --user-group --home-dir "$home" --no-create-home \
            --shell /usr/sbin/nologin "$user"
    fi
done
install -d -m 0755 "$ROOT" "$ROOT/releases" "$ROOT/native" "$ROOT/proxy"
install -d -m 0700 -o meyo-legion -g meyo-legion "$STATE"
install -d -m 0700 -o meyo-proxy -g meyo-proxy /var/lib/meyo-proxy
install -d -m 0750 -o meyo-proxy -g meyo-proxy /var/log/meyo-proxy
STAGE="$(mktemp -d "$ROOT/.prepare.XXXXXX")"
trap '[[ -z "${STAGE:-}" ]] || rm -rf -- "$STAGE"' EXIT
mkdir "$STAGE/app" "$STAGE/codex" "$STAGE/caddy"
tar -xzf "$BUNDLE/app.tar.gz" -C "$STAGE/app" --strip-components=1 --no-same-owner
tar -xzf "$BUNDLE/codex.tar.gz" -C "$STAGE/codex" --no-same-owner
tar -xzf "$BUNDLE/caddy.tar.gz" -C "$STAGE/caddy" --no-same-owner
if [[ -e "$ROOT/releases/$REVISION" ]]; then
    diff -qr "$STAGE/app" "$ROOT/releases/$REVISION" >/dev/null ||
        { echo "现有固定程序与部署包不同，未覆盖。" >&2; exit 2; }
else
    mv "$STAGE/app" "$ROOT/releases/$REVISION"
fi
install -m 0755 "$STAGE/codex/codex-x86_64-unknown-linux-musl" "$ROOT/native/codex"
install -m 0755 "$STAGE/caddy/caddy" "$ROOT/proxy/caddy"
[[ "$("$ROOT/native/codex" --version)" == "codex-cli 0.153.4" ]]
[[ "$("$ROOT/proxy/caddy" version)" == v2.11.7* ]]
# Ubuntu 24.04 otherwise moves unconfined bwrap into a profile that rejects uid_map.
# Scope the exception to our immutable executable and its inherited process chain.
CODEX_APPARMOR_PROFILE=/etc/apparmor.d/meyo-legion-codex
if [[ "$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || true)" == 1 ]]; then
    [[ ! -L "$CODEX_APPARMOR_PROFILE" ]] ||
        { echo "专用 AppArmor 路径是符号链接，未覆盖。" >&2; exit 2; }
    if [[ -e "$CODEX_APPARMOR_PROFILE" ]] &&
       ! cmp -s "$HERE/meyo-codex.apparmor" "$CODEX_APPARMOR_PROFILE"; then
        echo "已有同名 AppArmor 策略不同，未覆盖。" >&2
        exit 2
    fi
    command -v apparmor_parser >/dev/null ||
        { echo "缺少 AppArmor 加载器，未关闭全局限制。" >&2; exit 2; }
    install -m 0644 "$HERE/meyo-codex.apparmor" "$CODEX_APPARMOR_PROFILE"
    apparmor_parser -r "$CODEX_APPARMOR_PROFILE"
fi
# Restores the already-declared built-in CLI; does not register/install a new member.
if [[ ! -x "$ROOT/venv/bin/python" ]]; then
    python3.12 -m venv "$ROOT/venv"
fi
REQ="$ROOT/releases/$REVISION/release/legion-service/requirements-core.txt"
REQ_HASH="$(sha256sum "$REQ" | cut -d' ' -f1)"
if [[ ! -f "$ROOT/core-requirements.sha256" ]]; then
    timeout 900 "$ROOT/venv/bin/python" -m pip install -r "$REQ"
elif [[ "$(cat "$ROOT/core-requirements.sha256")" != "$REQ_HASH" ]]; then
    echo "已有核心依赖输入不同，未原地更新；请走受控升级。" >&2
    exit 2
fi
"$ROOT/venv/bin/python" -m pip check
"$ROOT/venv/bin/python" -m pip freeze > "$ROOT/core-resolved.txt"
printf '%s\n' "$REQ_HASH" > "$ROOT/core-requirements.sha256"
make_software_readable "$ROOT/releases/$REVISION" "$ROOT/venv"
[[ -e "$ROOT/current" ]] || ln -s "$ROOT/releases/$REVISION" "$ROOT/current"
runuser -u meyo-legion -- "$ROOT/venv/bin/python" -I "$ROOT/current/meyo.py" \
    init --state-dir "$STATE" --user "$OWNER"
runuser -u meyo-legion -- "$ROOT/venv/bin/python" -I "$ROOT/current/meyo.py" \
    configure-public-origin --state-dir "$STATE" --origin "$ORIGIN"
# Caddy runs as meyo-proxy; root's private umask must not hide optional alias imports.
install -d -m 0755 /etc/meyo-legion /etc/meyo-legion/aliases
printf 'MEYO_PUBLIC_HOST=%s\nMEYO_UPSTREAM=127.0.0.1:8000\nMEYO_ACCESS_LOG=/var/log/meyo-proxy/access.json\n' \
    "$PUBLIC_HOST" > /etc/meyo-legion/proxy.env
install -m 0644 "$HERE/Caddyfile" /etc/meyo-legion/Caddyfile
install -m 0644 "$HERE/meyo-legion.service" "$HERE/meyo-proxy.service" /etc/systemd/system/
install -m 0750 "$HERE/backup.sh" "$ROOT/backup.sh"
runuser -u meyo-proxy -- env MEYO_PUBLIC_HOST="$PUBLIC_HOST" \
    MEYO_UPSTREAM=127.0.0.1:8000 MEYO_ACCESS_LOG=/var/log/meyo-proxy/access.json \
    XDG_DATA_HOME=/var/lib/meyo-proxy "$ROOT/proxy/caddy" \
    validate --config /etc/meyo-legion/Caddyfile
systemctl daemon-reload
systemctl enable --now meyo-legion.service meyo-proxy.service
echo "服务已交给 systemd 启动；模型授权、公网 TLS、真实任务、设备和恢复仍需实际验收。"
