#!/usr/bin/env bash
#
# ToOak 日常启动脚本。
#
#   ./tooak.sh                  # 打开仓库里的 library/（没有就用 ~/ToOakLibrary）
#   ./tooak.sh --serve          # 顺便开 Web 审阅端；其它参数原样交给 run.py
#   TOOAK_LIBRARY=~/别的库 ./tooak.sh
#
# 脚本自己处理的小事：
#   * cd 到仓库再启动（放个软链到 ~/.local/bin 也能用，路径会自动解开）
#   * 优先用仓库里的 .venv，其次 python3；缺 PySide6 时给人话提示
#   * 单实例：同一时刻只开一个窗口（要多开用 TOOAK_ALLOW_MULTIPLE=1）
#   * 输出同时写日志，崩了自动把最后几行打出来
#
# 脚本自己的开关（不传给程序）：
#   --launcher-help        看这份说明
#   --dry-run              只打印将要执行的命令，不启动
#   --log [行数]           看最近的日志（默认 40 行）
#   --install-desktop      往应用菜单里装一个 ToOak 图标（KDE/GNOME 都能搜到）
#   --uninstall-desktop    把上面那个图标删掉

set -euo pipefail

# ---------------------------------------------------------------- 基本位置

# 软链也要能解开：${BASH_SOURCE[0]} 是软链路径时，dirname 只会得到软链所在的目录
SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
REPO_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd -P)"
RUN_PY="$REPO_DIR/run.py"

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/tooak"
LOG_FILE="$STATE_DIR/tooak.log"
LOCK_FILE="$STATE_DIR/tooak.lock"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_FILE="$DESKTOP_DIR/tooak.desktop"
ICON_FILE="$REPO_DIR/tooak/desktop/assets/tooak128.png"

die() { printf 'ToOak：%s\n' "$*" >&2; exit 1; }

# 环境变量：新名字优先，旧的（TOAK_*）继续认，免得你脚本里写的老名字突然失效
env_or() {  # env_or TOOAK_X TOAK_X [默认值]
    local new_name="$1" old_name="$2" fallback="${3:-}"
    printf '%s' "${!new_name:-${!old_name:-$fallback}}"
}

# ---------------------------------------------------------------- 脚本自己的开关

show_help() {
    # 把文件开头的注释块原样当说明打印（遇到第一行非注释就停）
    awk 'NR > 1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$SCRIPT_PATH"
    printf '\n给程序的参数（原样传给 run.py）：\n'
    printf '  -L, --library <目录>   指定库（默认 %s/library）\n' "$REPO_DIR"
    printf '  --demo                 库为空时先造一份演示数据\n'
    printf '  --serve                启动时同时开 Web 审阅端\n'
}

# 注意：**不要**用 "-?" 这种通配来抓帮助 —— 那会把 -h/-L 这些给程序的参数一起吃掉
case "${1:-}" in
    --launcher-help|--usage) show_help; exit 0 ;;
esac

mkdir -p "$STATE_DIR"

case "${1:-}" in
    --log)
        lines="${2:-40}"
        [[ -f $LOG_FILE ]] || die "还没有日志：$LOG_FILE"
        tail -n "$lines" "$LOG_FILE"
        exit 0
        ;;
    --install-desktop)
        mkdir -p "$DESKTOP_DIR"
        # 用 bash 而不是 sh；Exec 里的 %u/%U 都不要，我们这个程序不吃 URL
        cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=ToOak
Name[zh_CN]=ToOak 文档管理器
Comment=个人资料与版本管理
Comment[zh_CN]=按「项目 → 事件 → 环节 → 文件」管理资料与各稿
Exec="$SCRIPT_PATH"
Icon=$ICON_FILE
Terminal=false
Categories=Office;Utility;
StartupWMClass=ToOak
Keywords=ToOak;文档;资料;版本;
EOF
        chmod 644 "$DESKTOP_FILE"
        command -v update-desktop-database >/dev/null 2>&1 \
            && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
        printf '已装到应用菜单：%s\n（KDE 里可能要在菜单里搜一次 "ToOak" 才会缓存下来）\n' "$DESKTOP_FILE"
        exit 0
        ;;
    --uninstall-desktop)
        rm -f "$DESKTOP_FILE"
        command -v update-desktop-database >/dev/null 2>&1 \
            && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
        printf '已从应用菜单移除。\n'
        exit 0
        ;;
esac

# ---------------------------------------------------------------- 解释器

[[ -f $RUN_PY ]] || die "找不到 $RUN_PY（这个脚本要放在仓库根目录，或做成软链）"

PYTHON="$(env_or TOOAK_PYTHON TOAK_PYTHON)"
if [[ -z $PYTHON ]]; then
    if [[ -x "$REPO_DIR/.venv/bin/python" ]]; then
        PYTHON="$REPO_DIR/.venv/bin/python"
    else
        PYTHON="$(command -v python3 || true)"
    fi
fi
[[ -n $PYTHON && -x $PYTHON ]] || die "找不到 python3；可以用 TOOAK_PYTHON=/path/to/python 指定"

if ! "$PYTHON" -c 'import PySide6' >/dev/null 2>&1; then
    die "$PYTHON 里没有 PySide6：用「$PYTHON -m pip install PySide6」装上，或用 TOOAK_PYTHON 指一个装好的解释器"
fi

# ---------------------------------------------------------------- 参数分流

# --dry-run 是脚本自己的开关，别传给程序（程序不认识它）；其余原样透传。
dry_run=0
passthrough=()
for arg in "$@"; do
    case "$arg" in
        --dry-run|--print-command) dry_run=1 ;;
        *) passthrough+=("$arg") ;;
    esac
done

# ---------------------------------------------------------------- 库目录

# 程序自己认 -L/--library 和 TOOAK_LIBRARY；这里只补一个"仓库里的库"这种默认值。
# 只要用户显式给了 -L，或者设了 TOOAK_LIBRARY，就一个字都不插嘴。
has_library_arg=0
for arg in ${passthrough[@]+"${passthrough[@]}"}; do
    case "$arg" in
        -L|--library|--library=*|-L*) has_library_arg=1 ;;
    esac
done
library_args=()
# 默认库的优先级：-L / TOOAK_LIBRARY > 上次用过的库（last-library）> 仓库里的 library/
# "上次用过的库"由界面里的「打开库…」写入 $STATE_DIR/last-library，
# 这样换过库位置之后，下次双击图标还是开那个库。
last_library_file="$STATE_DIR/last-library"
if [[ $has_library_arg -eq 0 && -z $(env_or TOOAK_LIBRARY TOAK_LIBRARY) ]]; then
    if [[ -f "$last_library_file" ]]; then
        last_dir="$(cat "$last_library_file" 2>/dev/null)"
        if [[ -n "$last_dir" && -f "$last_dir/tooak.sqlite3" ]]; then
            library_args=(-L "$last_dir")
        fi
    fi
    # 没记过、或者记的那个已经没了：退回仓库里的 library/（老库文件名也认）
    if [[ ${#library_args[@]} -eq 0 ]] \
       && { [[ -f "$REPO_DIR/library/tooak.sqlite3" ]] || [[ -f "$REPO_DIR/library/toak.sqlite3" ]]; }; then
        library_args=(-L "$REPO_DIR/library")
    fi
fi

# ---------------------------------------------------------------- 单实例

if [[ $(env_or TOOAK_ALLOW_MULTIPLE TOAK_ALLOW_MULTIPLE 0) != 1 ]]; then
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        printf 'ToOak 看起来已经在运行了。\n'
        printf '（要多开一个窗口：TOOAK_ALLOW_MULTIPLE=1 %s）\n' "$SCRIPT_PATH"
        exit 0
    fi
fi

# ---------------------------------------------------------------- 启动

command_line=("$PYTHON" "$RUN_PY" ${library_args[@]+"${library_args[@]}"} ${passthrough[@]+"${passthrough[@]}"})

if [[ $dry_run -eq 1 ]]; then
    printf '仓库：%s\n解释器：%s\n' "$REPO_DIR" "$PYTHON"
    printf '命令：'; printf '%q ' "${command_line[@]}"; printf '\n'
    exit 0
fi

cd -- "$REPO_DIR"

# 顺带记录这次启动（排查"某天打不开"时有用）
{
    printf '\n===== %s 启动 =====\n' "$(date '+%F %T')"
    printf '库：%s\n' "${library_args[1]:-$(env_or TOOAK_LIBRARY TOAK_LIBRARY 默认)}"
    printf '参数：%s\n' "${passthrough[*]:-（无）}"
} >> "$LOG_FILE"

# 日志给排查用；终端也照常显示（从桌面图标启动时终端是空的，不影响）
PYTHONUNBUFFERED=1 QT_LOGGING_RULES="${QT_LOGGING_RULES:-}" \
    "${command_line[@]}" 2>&1 | tee -a "$LOG_FILE"
status=${PIPESTATUS[0]}

if [[ $status -ne 0 ]]; then
    printf '\nToOak 退出码 %s。日志末尾：\n' "$status" >&2
    tail -n 15 "$LOG_FILE" >&2
    printf '完整日志：%s（用 %s --log 看）\n' "$LOG_FILE" "$SCRIPT_PATH" >&2
fi
exit "$status"
