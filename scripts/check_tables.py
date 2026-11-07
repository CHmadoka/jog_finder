"""查看数据表分布与任务38快照。"""
import sqlite3

con = sqlite3.connect("data/job_finder.db")
print("各表岗位数:", con.execute("select table_id, count(*) from job group by table_id").fetchall())
print("job51:", con.execute("select table_id, count(*) from job where platform='job51' group by table_id").fetchall())
print("liepin:", con.execute("select table_id, count(*) from job where platform='liepin' group by table_id").fetchall())
print("任务38快照:", con.execute("select filter_snapshot from crawl_task where id=38").fetchone()[0][:300])
print("任务38所属表:", con.execute("select id, platform, table_id is null from crawl_task where id in (37,38,39,40)").fetchall())