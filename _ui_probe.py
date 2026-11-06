import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True, channel="chrome")
        except Exception:
            browser = await p.chromium.launch(headless=True, channel="msedge")
        page = await browser.new_page()
        page.on("pageerror", lambda e: print("PAGEERROR:", str(e)[:150]))
        await page.goto("http://127.0.0.1:8765/", wait_until="networkidle")
        await page.wait_for_timeout(2500)

        # ---- 数据表管理弹窗 ----
        await page.locator(".toolbar .table-combo .combo-setting").click()
        await page.wait_for_timeout(700)
        dlg = page.locator(".el-dialog").filter(has_text="数据表管理")
        print("table dialog visible:", await dlg.count())
        rows = dlg.locator(".dlg-row")
        print("rows:", await rows.count())
        # 点击第一行(默认表)的编辑
        await rows.first.locator(".act-btn", has_text="编辑").click()
        await page.wait_for_timeout(500)
        editrow = rows.first
        print("edit row html:")
        print((await editrow.inner_text()).replace("\n", " | "))
        inputs = await editrow.locator("input").count()
        btns = await editrow.locator("button").all_inner_texts()
        print("inputs:", inputs, "buttons:", btns)
        # 关闭再打开
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)
        await page.locator(".toolbar .table-combo .combo-setting").click()
        await page.wait_for_timeout(700)
        first_row_text = await rows.first.inner_text()
        print("重开后首行文本:", first_row_text.replace("\n", " | ")[:80])

        # ---- 岗位设置弹窗(从抓取面板齿轮进入) ----
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(400)
        gear_combo = page.locator(".side .table-combo .combo-setting")
        await gear_combo.click()
        await page.wait_for_timeout(700)
        gdlg = page.locator(".el-dialog").filter(has_text="岗位设置管理")
        print("gear dialog visible:", await gdlg.count())
        grows = gdlg.locator(".dlg-row")
        print("gear rows:", await grows.count())
        # 编辑第一行
        await grows.first.locator(".act-btn", has_text="编辑").click()
        await page.wait_for_timeout(500)
        print("gear edit row text:", (await grows.first.inner_text()).replace("\n", " | "))
        gbtns = await grows.first.locator("button").all_inner_texts()
        print("gear edit buttons:", gbtns)
        # 添加按钮状态(编辑中)
        addbtn = gdlg.locator("button", has_text="添加")
        print("添加按钮 disabled:", await addbtn.first.is_disabled())
        # 关闭重开
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(400)
        await gear_combo.click()
        await page.wait_for_timeout(700)
        print("gear 重开后首行:", (await grows.first.inner_text()).replace("\n", " | ")[:60])
        print("添加按钮 disabled(重开):", await addbtn.first.is_disabled())
        await browser.close()

asyncio.run(main())
