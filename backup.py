"""将最近七天未备份的五彩高亮逐条保存到 Snapnote。"""

import datetime as dt
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

CHINA = ZoneInfo("Asia/Shanghai")
REMOTE = "https://github.com/v-yutianran/wucai-to-snapnote.git"
SKILL_SCRIPT = Path.home() / ".agents/skills/wucai/scripts/wucai_api.py"


class BackupError(Exception):
    """可向终端展示且不含服务响应或凭据的错误。"""


def normalize_id(value):
    if type(value) not in (int, str) or not re.fullmatch(r"[0-9]+", str(value)) or int(value) <= 0:
        raise BackupError("h_id 必须是正整数或十进制字符串")
    return str(int(value))


def marker(h_id, region):
    return f"[wucai:{region}:h_id:{normalize_id(h_id)}]"


def body_digest(text):
    return hashlib.sha256(text.replace("\r\n", "\n").rstrip("\n").encode("utf-8")).hexdigest()


def render_note(highlight, region):
    h_id = normalize_id(highlight.get("h_id"))
    values = {}
    for key in ("content", "annotation", "annonation", "tags", "image_url"):
        value = highlight.get(key)
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise BackupError("高亮文本字段格式无效")
        values[key] = value
    created = "未提供"
    if highlight.get("created_at") is not None:
        try:
            created = dt.datetime.fromtimestamp(highlight["created_at"], CHINA).strftime("%Y-%m-%d %H:%M:%S %z")
        except (ValueError, TypeError, OverflowError, OSError):
            raise BackupError("高亮原始时间格式无效") from None
    return (f"五彩高亮 · {h_id}\n\n原文：\n{values['content']}\n\n"
            f"批注：\n{values['annotation'] or values['annonation']}\n\n"
            f"原始时间：{created}\n标签：{values['tags']}\n图片链接：{values['image_url']}\n"
            f"来源：五彩 {region}\n{marker(h_id, region)}")


def load_records(root):
    records = root / "records"
    if records.is_symlink():
        raise BackupError("records 不能是软链接")
    saved = set()
    for file in sorted(records.glob("*.json")):
        try:
            if file.is_symlink() or dt.date.fromisoformat(file.stem).isoformat() != file.stem:
                raise ValueError()
            values = json.loads(file.read_text(encoding="utf-8"))
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ValueError()
            saved.update(normalize_id(value) for value in values)
        except (ValueError, OSError, BackupError):
            raise BackupError("每日 JSON 记录损坏或名称无效，请修复后再运行") from None
    return saved


def atomic_json(file, value):
    file.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".backup-", dir=file.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, file)
        directory_fd = os.open(file.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def backup_items(root, highlights, region, call, today):
    saved = load_records(root)
    prepared = {normalize_id(item.get("h_id")): render_note(item, region) for item in highlights}
    stats = {"created": 0, "recovered": 0, "skipped": 0}
    pending_file = root / ".runtime/pending.json"
    if pending_file.parent.is_symlink() or pending_file.is_symlink():
        raise BackupError("运行状态不能是软链接")

    def confirm(pending):
        h_id = pending["h_id"]
        uuid = pending["note_uuid"] or find_note(call, h_id, region)
        if not uuid:
            raise BackupError("待确认笔记尚未找到；请人工核对 pending.json，不会自动重建")
        text = note_text(call, uuid)
        if marker(h_id, region) not in text.splitlines() or body_digest(text) != pending["digest"]:
            raise BackupError("笔记读回不一致；保留 pending.json，请人工核对")
        file = root / "records" / f"{today()}.json"
        values = json.loads(file.read_text(encoding="utf-8")) if file.exists() else []
        atomic_json(file, sorted(set(values) | {h_id}, key=int))
        saved.add(h_id)
        pending_file.unlink(missing_ok=True)

    if pending_file.exists():
        try:
            pending = json.loads(pending_file.read_text(encoding="utf-8"))
            if (not isinstance(pending, dict) or pending.get("region") != region
                    or not re.fullmatch(r"[0-9a-f]{64}", pending.get("digest", ""))
                    or (pending.get("note_uuid") is not None and not isinstance(pending["note_uuid"], str))):
                raise ValueError()
            pending["h_id"] = normalize_id(pending.get("h_id"))
        except (ValueError, TypeError, OSError, BackupError):
            raise BackupError("pending.json 无效，请人工核对后再运行") from None
        if pending["h_id"] not in saved:
            confirm(pending)
            stats["recovered"] += 1
        else:
            pending_file.unlink()

    for h_id, text in prepared.items():
        if h_id in saved:
            stats["skipped"] += 1
            continue
        existing = find_note(call, h_id, region)
        pending = {"h_id": h_id, "region": region, "digest": body_digest(text), "note_uuid": existing}
        atomic_json(pending_file, pending)
        if not existing:
            result = call("memo_create", {"body": {"delta": {"ops": [{"insert": text + "\n"}]}},
                                          "response_mode": "minimal"})
            uuid = result.get("note_uuid")
            if not isinstance(uuid, str) or not uuid.strip():
                raise BackupError("创建结果没有有效 note_uuid；保留 pending.json，请人工核对")
            pending["note_uuid"] = uuid
            atomic_json(pending_file, pending)
        confirm(pending)
        stats["recovered" if existing else "created"] += 1
    return stats


def note_text(call, uuid):
    detail = call("memo_get", {"note_uuid": uuid, "response_mode": "editing",
                               "include_ocr": False, "include_relations": False})
    body = detail.get("item", detail.get("note_detail", detail)).get("body", {})
    if "delta" in body:
        delta = body["delta"]
        ops = delta.get("ops") if isinstance(delta, dict) else None
        if not isinstance(ops, list) or not all(isinstance(op, dict) and isinstance(op.get("insert"), str) for op in ops):
            raise BackupError("笔记读回包含无效 Delta 或非文本内容")
        return "".join(op["insert"] for op in ops)
    text = body.get("plain_text")
    if not isinstance(text, str):
        raise BackupError("笔记读回缺少完整纯文本正文")
    return text


def find_note(call, h_id, region):
    query = marker(h_id, region)
    matches, seen, offset = set(), set(), 0
    while True:
        result = call("memo_search", {"query": query, "include_archived": True,
                                      "offset": offset, "limit": 20, "response_mode": "compact"})
        items = result.get("items")
        if not isinstance(items, list) or type(result.get("has_more")) is not bool:
            raise BackupError("Snapnote 搜索分页格式无效")
        new_items = set()
        for item in items:
            uuid = item.get("uuid")
            if not isinstance(uuid, str) or not uuid:
                raise BackupError("搜索结果缺少笔记 UUID")
            new_items.add(uuid)
            if query in note_text(call, uuid).splitlines():
                matches.add(uuid)
        if not result["has_more"]:
            break
        if not new_items - seen:
            raise BackupError("Snapnote 搜索分页未推进")
        seen.update(new_items)
        offset += len(items)
    if len(matches) > 1:
        raise BackupError("同一 h_id 匹配多篇笔记，请人工核对")
    return next(iter(matches), None)


def fetch_highlights(call):
    cursor, cursors, items = "", {""}, {}
    while True:
        data = call({"range": "7d", "cursor": cursor, "page_size": 30})
        rows, next_cursor = data.get("highlights"), data.get("next_cursor")
        if "highlights" in data and rows is None and next_cursor == "":
            rows = []
        if not isinstance(rows, list) or not isinstance(next_cursor, str):
            raise BackupError("五彩高亮分页格式无效")
        for item in rows:
            if not isinstance(item, dict):
                raise BackupError("五彩高亮记录格式无效")
            items.setdefault(normalize_id(item.get("h_id")), item)
        if not next_cursor:
            return list(items.values())
        if next_cursor in cursors:
            raise BackupError("五彩分页游标重复，已停止以免漏备份")
        cursors.add(next_cursor)
        cursor = next_cursor


def rpc(endpoint, token, name, arguments):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": f"snapnote.{name}", "arguments": arguments}}
    request = urllib.request.Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                              "Accept": "application/json"}, method="POST")
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
            data = json.load(response)
        if not isinstance(data, dict) or data.get("jsonrpc") != "2.0" or data.get("id") != 1 or data.get("error"):
            raise ValueError()
        result = data.get("result")
        if not isinstance(result, dict) or result.get("isError"):
            raise ValueError()
        content = result.get("structuredContent")
        if not isinstance(content, dict) or content.get("is_error"):
            raise ValueError()
        return content
    except (OSError, ValueError, UnicodeError):
        raise BackupError(f"Snapnote {name} 请求或响应无效；已发出的创建请求需核对") from None


def skill_page(params, token, script=SKILL_SCRIPT):
    env = dict(os.environ, WUCAI_API_TOKEN=token, WUCAI_REGION="cn")
    try:
        result = subprocess.run([sys.executable, str(script), "list_highlights"], input=json.dumps(params),
                                env=env, capture_output=True, text=True, timeout=25, check=True)
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or data.get("code") != 1 or not isinstance(data.get("data"), dict):
            raise ValueError()
        return data["data"]
    except (OSError, ValueError, subprocess.SubprocessError):
        raise BackupError("五彩 Skill 请求失败，请核对 WUCAI_TOKEN、中国区账号及网络") from None


def git(root, *arguments):
    try:
        return subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True,
                              env=dict(os.environ, GIT_TERMINAL_PROMPT="0"), timeout=45, check=True).stdout.rstrip("\n")
    except (OSError, subprocess.SubprocessError):
        raise BackupError(f"Git {arguments[0]} 失败，请在仓库中检查网络、认证或分支；本地记录保留") from None


def daily_path(name):
    if not re.fullmatch(r"records/[0-9]{4}-[0-9]{2}-[0-9]{2}\.json", name):
        return False
    try:
        dt.date.fromisoformat(Path(name).stem)
        return True
    except ValueError:
        return False


def changed_records(root):
    entries = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    paths = []
    for entry in entries.split("\0"):
        if not entry:
            continue
        flags, name = entry[:2], entry[3:]
        if not daily_path(name) or any(flag in flags for flag in "DRCU!"):
            raise BackupError("存在备份记录以外的改动、删除或冲突，请先人工处理")
        paths.append(name)
    return paths


def git_preflight(root, expected_remote=REMOTE):
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root.resolve():
        raise BackupError("当前目录不是独立备份仓库")
    if git(root, "branch", "--show-current") != "main":
        raise BackupError("请在 main 分支运行")
    allowed = {expected_remote}
    if expected_remote.startswith("https://github.com/"):
        repository = expected_remote.removeprefix("https://github.com/")
        allowed.update({f"git@github.com:{repository}", f"ssh://git@github.com/{repository}"})
    push_urls = git(root, "remote", "get-url", "--push", "--all", "origin").splitlines()
    if (git(root, "remote", "get-url", "origin") not in allowed
            or len(push_urls) != 1 or push_urls[0] not in allowed):
        raise BackupError("origin 不匹配已授权的备份仓库")
    changed_records(root)
    load_records(root)
    remote = git(root, "ls-remote", "--exit-code", "origin", "refs/heads/main").split()[0]
    git(root, "merge-base", "--is-ancestor", remote, "HEAD")
    for commit in git(root, "rev-list", "--reverse", f"{remote}..HEAD").splitlines():
        parents = git(root, "rev-list", "--parents", "-n", "1", commit).split()
        changes = [line.split("\t") for line in git(root, "diff-tree", "--no-commit-id", "--name-status", "-r", commit).splitlines()]
        if (len(parents) != 2 or not changes
                or not all(len(change) == 2 and change[0] in ("A", "M") and daily_path(change[1]) for change in changes)):
            raise BackupError("存在备份记录以外的未推送提交，请先人工同步代码")
    return remote


def sync_records(root, expected_remote=REMOTE):
    remote = git_preflight(root, expected_remote)
    paths = changed_records(root)
    if paths:
        git(root, "add", "--", *paths)
        git(root, "commit", "--only", "-m", "chore(backup): 记录已确认的五彩高亮备份", "--", *paths)
    head = git(root, "rev-parse", "HEAD")
    if head != remote:
        git(root, "push", "--dry-run", "origin", "HEAD:refs/heads/main")
        git(root, "push", "origin", "HEAD:refs/heads/main")
        if git(root, "ls-remote", "--exit-code", "origin", "refs/heads/main").split()[0] != head:
            raise BackupError("远端 HEAD 核验失败，本地记录保留")


@contextlib.contextmanager
def run_lock(root):
    runtime = root / ".runtime"
    if runtime.is_symlink():
        raise BackupError("运行目录不能是软链接")
    runtime.mkdir(exist_ok=True)
    # ponytail: 单机文件锁；多机并发需要服务端幂等键或共享锁。
    fd = os.open(runtime / "run.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("已有备份进程在运行") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def settings(env):
    wucai, token = env.get("WUCAI_TOKEN", "").strip(), env.get("SNAPNOTE_MCP_TOKEN", "").strip()
    if not wucai.startswith("wct-") or any(c in wucai for c in "\r\n"):
        raise BackupError("请在启动终端中 export WUCAI_TOKEN；不会使用旧变量或加载 .zshrc")
    if not token or any(c in token for c in "\r\n"):
        raise BackupError("请在启动终端中配置 SNAPNOTE_MCP_TOKEN")
    endpoint = env.get("SNAPNOTE_MCP_URL", "").strip()
    if not endpoint:
        try:
            cfg = tomllib.loads((Path.home() / ".codex/config.toml").read_text(encoding="utf-8"))
            endpoint = cfg["mcp_servers"]["snapnote"]["url"]
        except (OSError, ValueError, KeyError, TypeError):
            raise BackupError("请配置 SNAPNOTE_MCP_URL 或本机 Codex 的 Snapnote MCP 地址") from None
    try:
        url = urllib.parse.urlsplit(endpoint)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError()
        url.port
    except (ValueError, TypeError):
        raise BackupError("Snapnote 地址无效；仅使用 HTTP/HTTPS 地址和单一 Bearer 认证") from None
    return wucai, endpoint, token


def event(name, **fields):
    print(json.dumps({"event": name, **fields}, ensure_ascii=False), flush=True)


def main(argv=None, root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只读预览，不创建笔记、不写记录、不提交或推送")
    args = parser.parse_args(argv)
    root = Path(root or Path(__file__).resolve().parent)
    try:
        wucai, endpoint, token = settings(os.environ)
        if not SKILL_SCRIPT.is_file():
            raise BackupError("未找到本机五彩 Skill：请先安装 wucai Skill")
        call = lambda name, body: rpc(endpoint, token, name, body)
        lock = contextlib.nullcontext() if args.dry_run else run_lock(root)
        with lock:
            saved = load_records(root)
            remote = git_preflight(root, REMOTE)
            if not args.dry_run:
                sync_records(root, REMOTE)
            profile = call("service_profile", {})
            if profile.get("service_key") != "snapnote.data_source" or profile.get("service_version") != "v1alpha1":
                raise BackupError("Snapnote 服务契约不匹配，本脚本仅支持 v1alpha1 JSON HTTP 接口")
            highlights = fetch_highlights(lambda params: skill_page(params, wucai, SKILL_SCRIPT))
            # 在任何创建请求之前校验本次全部正文，避免坏数据导致部分写入。
            for item in highlights:
                render_note(item, "cn")
            if args.dry_run:
                event("backup.preview", source_count=len(highlights),
                      unrecorded_count=sum(normalize_id(item["h_id"]) not in saved for item in highlights),
                      pending_confirmation=(root / ".runtime/pending.json").exists(),
                      git_sync_needed=bool(changed_records(root)) or git(root, "rev-parse", "HEAD") != remote)
                return 0
            event("backup.started", source_count=len(highlights))
            failure = None
            try:
                stats = backup_items(root, highlights, "cn", call, lambda: dt.datetime.now(CHINA).date().isoformat())
            except (Exception, KeyboardInterrupt) as error:
                failure = error
            try:
                sync_records(root, REMOTE)
            except BackupError as error:
                event("backup.git_sync_failed", message=str(error))
                failure = failure or error
            if failure:
                raise failure
            event("backup.completed", source_count=len(highlights), **stats, git_synced=True)
            return 0
    except BackupError as error:
        event("backup.failed", message=str(error))
    except KeyboardInterrupt:
        event("backup.cancelled", message="已停止，已确认记录及待确认状态保留")
    except Exception:
        event("backup.failed", message="本地文件或接口数据异常；已停止，请核对记录及 pending.json")
    return 1


if __name__ == "__main__":
    sys.exit(main())
