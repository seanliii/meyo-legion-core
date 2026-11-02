# Superagent CLI：保留用户暂停事实

适用上游：`deepseek-ai/dsh-superagent-prototype`，
commit **`6337fc823ce20251c8de8085948165c81aaa701e`**。
原 `scripts/dsh-superagent` SHA-256：
`e561ea08c2e017467562cfa55012147c2dd0d20fe872dcbb241be8cfee8985da`。

**尚未上游合入；不会自动应用；没有更新运行中的安装。**
这是两行诊断投影补丁，不代表 Superagent worker 能调用军团。

## 改动边界

- API 的 `paused` 为布尔值时原样传回；字段缺失或不是布尔值时为 JSON `null`（未知），不假定为 `false`。
- 保留 `manually_stopped`，不混淆业务暂停与停止服务。
- 健康但用户暂停仍返回 exit 0；原 setup、组件错误、不可达等退出语义不变。
- 不增加请求、工具、身份或权限，不改 worker、状态机、配置或自动化。

## 手动应用与回退

先把 `PATCH_DIR` 设为本目录，把 `SUPERAGENT_SOURCE_ROOT` 设为核验过上述版本的**受控上游副本**，不要指向运行中的安装。

```sh
cd "$SUPERAGENT_SOURCE_ROOT"
git apply --check "$PATCH_DIR/0001-preserve-paused-in-status.patch"
git apply "$PATCH_DIR/0001-preserve-paused-in-status.patch"

# 需要回退时：
git apply --reverse --check "$PATCH_DIR/0001-preserve-paused-in-status.patch"
git apply --reverse "$PATCH_DIR/0001-preserve-paused-in-status.patch"
```

只对这一个版本验证过；其他版本须重新核源码并检查补丁，不能强行套用。

## 离线回归

使用 Python 标准库 unittest，无需安装依赖。测试只加载指定副本的原脚本函数；
HTTP、进程列表和停止标记均为内存替身，不调用脚本 `main/start/stop`，不启动服务或读取凭据。

```sh
export SUPERAGENT_SOURCE_ROOT
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s "$PATCH_DIR" -p test_status_paused.py -v
```

六项测试覆盖布尔真/假、缺字段和非布尔值、setup 等待、组件/运行错误、
不可达/停止及暂停与停止标记的区别。补丁前应因 `paused` 缺失而失败，补丁后应全部通过。
本目录仅发布补丁、方法和回归测试；不包含完整上游脚本、私人 QA 日志或实例数据。
