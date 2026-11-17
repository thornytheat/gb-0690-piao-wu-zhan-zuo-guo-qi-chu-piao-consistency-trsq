# 运行维护

主服务端口与Compose保持一致。数据保存在records命名卷，应用代码不需要写入权限。

```sh
docker compose logs --tail=80 app
docker compose restart app
docker compose exec -T app pytest -q
docker compose down --remove-orphans
```

健康检查GET /health。失败输入返回原因码，结构性错误422，过大文件413，陈旧复核409，切换失败503。
文件内容最大4MB。批次先计算再原子保存，失败不留半条记录。证据CSV按原始行号定位。

## 补传与版本切换

补传在M7提交：`POST /api/supplement/<原批次ID>`，JSON为 `{"text": "CSV"}` 或表单文件 `file`。补传必须在当前版本上提交，否则返回409 STALE；重试同一补传返回 `created=false` 且不新增版本。成功后席态、票据有效性和渠道悬单随当前指针一次切换，原批次转只读并可继续回看。

`GET /api/lineage/<批次ID>` 返回谱系内各版本与当前版本，`GET /api/supplement/<批次ID>/diff` 返回原版与当前版差异，`GET /api/lineage/<批次ID>/diff.csv?base=…&current=…` 导出同一差异。

数据库为单文件 `runs.sqlite`。批次写入与指针切换在同一事务内；若进程在提交前退出，重启后读取会按最高revision恢复当前指针。不要手工同时修改 `runs` 与 `lineages` 表；从备份恢复后首次读取会自动补建缺失谱系并重排重复版本号。
