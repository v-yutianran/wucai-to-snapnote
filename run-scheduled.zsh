#!/bin/zsh
# 使用非交互登录环境，仅在需要时加载五彩令牌的导出行。
exec /bin/zsh -lc '
  if [[ -z "${WUCAI_TOKEN:-}" ]]; then
    source <(/usr/bin/sed -nE "/^[[:space:]]*export[[:space:]]+WUCAI_TOKEN[[:space:]]*=/p" "${ZDOTDIR:-$HOME}/.zshrc")
  fi
  exec /Applications/ServBay/package/python/3.11/3.11.16/Python.framework/Versions/Current/bin/python3 /Users/xinwu/project/wucai-to-snapnote/backup.py "$@"
' wucai-to-snapnote "$@"
