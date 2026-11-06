# 一次性生成 web/cities.js:省→市两级级联数据(源自 china-division,npmmirror 镜像)
# 产出结构:window.CN_PROVINCE_CITIES = [{value,label,children:[{value,label,leaf:true}]}]
# 叶子 value 用城市短名(去"市"后缀),与各平台 city_codes 的键风格一致
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
provinces = json.loads((BASE / "data" / "_prov.json").read_text(encoding="utf-8"))
cities = json.loads((BASE / "data" / "_city.json").read_text(encoding="utf-8"))

MUNI = {"北京市", "上海市", "天津市", "重庆市"}


def short_name(name: str) -> str:
    """行政区短名:省去掉 省/市/特别行政区 等后缀;自治区保留习惯简称。"""
    for suf in ("维吾尔自治区", "壮族自治区", "回族自治区", "自治区", "特别行政区", "省", "市"):
        if name.endswith(suf) and len(name) > len(suf) + 1:
            return name[: -len(suf)]
    return name


by_prov: dict[str, list[dict]] = {}
for c in cities:
    # 跳过"省直辖县级行政区划"等虚级节点,不是可搜索的城市
    if "直辖县" in c["name"] or "自治区直辖" in c["name"]:
        continue
    by_prov.setdefault(c["provinceCode"], []).append(c["name"])

out = []
for p in provinces:
    prov_full, pcode = p["name"], p["code"]
    prov_short = short_name(prov_full)
    kids = by_prov.get(pcode, [])
    if prov_full in MUNI:
        # 直辖市:下属"市辖区"虚级 → 城市选项即省短名(北京/上海/…)
        children = [{"value": prov_short, "label": prov_full, "leaf": True}]
    elif not kids:
        # 无市级数据的省(如港澳台若后续加入):省本身作为可选叶子
        children = [{"value": prov_short, "label": prov_full, "leaf": True}]
    else:
        children = [
            {"value": short_name(n) if n != "市辖区" else prov_short,
             "label": n if n != "市辖区" else prov_full, "leaf": True}
            for n in sorted(kids)
        ]
    out.append({"value": prov_full, "label": prov_full, "children": children})

# 去重防御:同一省内叶子 value 不应重复
for prov in out:
    vals = [k["value"] for k in prov["children"]]
    assert len(vals) == len(set(vals)), f"{prov['label']} 存在重复城市短名: {vals}"

target = BASE / "web" / "cities.js"
target.write_text(
    "/* 全国省→市两级级联数据(生成自 china-division@latest,npmmirror 镜像;"
    " 重新生成用 scripts/gen_cities.py)。叶子 value 为城市短名,匹配平台 city_codes。 */\n"
    "window.CN_PROVINCE_CITIES = " + json.dumps(out, ensure_ascii=False, separators=(",", ":")) + ";\n",
    encoding="utf-8",
)
n_cities = sum(len(p["children"]) for p in out)
print(f"生成 {target.name}: {len(out)} 省 / {n_cities} 市选项")
