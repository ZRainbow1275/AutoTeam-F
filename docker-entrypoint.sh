#!/bin/bash
set -e

# 清理残留锁文件并启动虚拟显示器
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1280x800x24 &
export DISPLAY=:99

# 确保数据目录存在且可写
mkdir -p /app/data /app/data/auths /app/data/screenshots
chmod -R 777 /app/data

# 数据文件：无条件软链到 data/（确保所有写入都持久化）
for f in .env accounts.json state.json; do
    # data 里没有就创建空文件
    [ -f "/app/data/$f" ] || touch "/app/data/$f"
    # 删除容器内的真实文件（如果不是软链），然后建软链
    rm -f "/app/$f"
    ln -s "/app/data/$f" "/app/$f"
done

# 目录软链
for d in auths screenshots; do
    rm -rf "/app/$d"
    ln -s "/app/data/$d" "/app/$d"
done

# 执行命令
exec uv run autoteam "$@"
