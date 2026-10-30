#!/usr/bin/env bash
# A consistent maintenance backup. Never stops an active service itself.
set -euo pipefail
umask 077
[[ "$EUID" == 0 ]] || { echo "请由本实例运维用户以 root 执行。" >&2; exit 2; }
if [[ "$(systemctl show meyo-legion.service -p ActiveState --value)" != inactive ]] ||
   [[ "$(systemctl show meyo-legion.service -p MainPID --value)" != 0 ]] ||
   [[ "$(systemctl show meyo-legion.service -p ControlPID --value)" != 0 ]]; then
    echo "服务仍在运行。先等真实任务结束并正常停服；没有发送停止信号。" >&2
    exit 2
fi
STATE=/var/lib/meyo-legion
[[ -d "$STATE" && ! -L "$STATE" && -f "$STATE/config.json" ]] ||
    { echo "没有核定的实例数据，未备份其他目录。" >&2; exit 2; }
[[ ! -L /var/lib/meyo-backup && ! -L /var/backups/meyo-legion ]] || exit 2
install -d -m 0700 /var/lib/meyo-backup /var/backups/meyo-legion
export RESTIC_REPOSITORY=/var/backups/meyo-legion
export RESTIC_PASSWORD_FILE=/var/lib/meyo-backup/password
[[ ! -L "$RESTIC_PASSWORD_FILE" ]] || exit 2
if [[ ! -e "$RESTIC_PASSWORD_FILE" ]]; then
    [[ ! -e "$RESTIC_REPOSITORY/config" ]] ||
        { echo "已有备份但缺原密码，没有重置仓库。" >&2; exit 2; }
    python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > "$RESTIC_PASSWORD_FILE"
fi
[[ -f "$RESTIC_PASSWORD_FILE" && -s "$RESTIC_PASSWORD_FILE" &&
   "$(stat -c %U "$RESTIC_PASSWORD_FILE")" == root ]] || exit 2
chmod 0600 "$RESTIC_PASSWORD_FILE"
[[ -e "$RESTIC_REPOSITORY/config" ]] || restic init
restic backup "$STATE" --tag meyo-quiescent
echo "已写入本机加密备份；异机副本与实际恢复演练仍须完成，不能据此承诺灾备。"
