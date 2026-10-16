#!/bin/zsh
# 从登录环境读取 .zshrc 中的凭据；参数原样传给备份脚本。
exec /bin/zsh -lic 'exec /Applications/ServBay/package/python/3.11/3.11.16/Python.framework/Versions/Current/bin/python3 /Users/xinwu/project/wucai-to-snapnote/backup.py "$@"' wucai-to-snapnote "$@"
