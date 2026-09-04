import { test, expect } from "@playwright/test";

test("真实数据：查询、证据、相邻实体、筛选、恢复", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(page.locator(".stat").nth(2).locator("strong")).not.toHaveText("0");
  await expect(page.locator(".graph-canvas canvas").first()).toBeVisible();
  await expect(page.locator(".limit-notice")).toContainText("为保持交互流畅");
  await page.getByRole("textbox", { name: "搜索实体名称" }).fill("猪瘟");
  await page.getByRole("button", { name: "猪瘟 疾病", exact: true }).click();
  await expect(page.locator(".entity-name")).toHaveText("猪瘟");
  await page.locator(".relation-group").filter({ hasText: "常见症状" }).locator(".relation-link").first().click();
  await expect(page.getByRole("heading", { name: "关系证据" })).toBeVisible();
  await expect(page.locator(".evidence")).toContainText("doc_000022");
  await expect(page.locator(".evidence")).toContainText("腹泻");
  await page.getByRole("button", { name: "返回实体详情" }).click();
  await page.locator(".neighbor").filter({ hasText: "腹泻" }).click();
  await expect(page.locator(".entity-name")).toHaveText("腹泻");
  await page.getByRole("button", { name: "放大图谱" }).click();
  await page.getByRole("button", { name: "缩小图谱" }).click();
  await page.getByRole("button", { name: "适应画布" }).click();
  await page.getByRole("button", { name: "恢复默认" }).click();
  await expect(page.getByRole("textbox", { name: "搜索实体名称" })).toHaveValue(
    "",
  );
  await page
    .getByRole("checkbox", { name: /^症状 / })
    .first()
    .uncheck();
  await page.getByRole("textbox", { name: "搜索实体名称" }).fill("腹泻");
  await expect(
    page.getByRole("button", { name: "腹泻 症状", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "恢复默认" }).click();
  expect(errors).toEqual([]);
  await page.screenshot({
    path: "test-results/workbench-desktop.png",
    fullPage: true,
  });
});

test("错误重试与空数据状态", async ({ page }) => {
  let failed = true;
  await page.route("**/data/triples.json", (route) =>
    route.fulfill({
      status: failed ? 503 : 200,
      contentType: "application/json",
      body: failed ? "{}" : "[]",
    }),
  );
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText("503");
  failed = false;
  await page.getByRole("button", { name: "重新加载" }).click();
  await expect(
    page.getByText("数据中暂无有效三元组。", { exact: false }),
  ).toBeVisible();
});

test("非法记录、80 节点限制和来源文本不执行 HTML", async ({ page }) => {
  const rows = Array.from({ length: 100 }, (_, index) => ({
    subject: "中心",
    subject_type: "疾病",
    relation: "关联",
    object: `节点${index}`,
    object_type: "症状",
    source_text: '<img src=x onerror="alert(1)">',
    source_document_id: `doc_${index}`,
  }));
  await page.route("**/data/triples.json", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify([...rows, {}]),
    }),
  );
  await page.goto("/");
  await expect(page.getByText("已跳过 1 条", { exact: false })).toBeVisible();
  await page.getByRole("textbox", { name: "搜索实体名称" }).fill("中心");
  await page.getByRole("button", { name: "中心 疾病", exact: true }).click();
  await expect(page.locator(".limit-notice")).toContainText("101 个实体");
  await expect(page.locator(".graph-footer")).toContainText("80");
  await page.locator(".relation-link").first().click();
  await expect(page.locator("blockquote")).toContainText("<img");
  await expect(page.locator("blockquote img")).toHaveCount(0);
});

test("窄屏和长中文名称不造成页面横向溢出", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.locator(".stats")).toBeVisible();
  await page.getByRole("textbox", { name: "搜索实体名称" }).fill("猪瘟");
  await page.getByRole("button", { name: "猪瘟 疾病", exact: true }).click();
  await expect(page.locator(".entity-name")).toContainText("猪瘟");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: "test-results/workbench-mobile.png",
    fullPage: true,
  });
});

test("视图切换、固定图例和可调可信度可用", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "图例与方向" }).click();
  await expect(page.getByRole("region", { name: "图例与关系方向说明" })).toContainText("主语指向宾语");
  await page.getByRole("button", { name: "可信筛选" }).click();
  await expect(page.getByLabel("最低置信度")).toBeVisible();
  await page.getByLabel("最低置信度").fill("0.8");
  await expect(page.locator(".confidence-control")).toContainText("80%");
  await page.getByRole("button", { name: "疾病—症状" }).click();
  await expect(page.locator(".mode-note")).toContainText("左侧为疾病");
  await page.getByRole("button", { name: "相似疾病" }).click();
  await expect(page.locator(".mode-note")).toContainText("前端派生分析");
});

test("高倍缩放保持标签与连线可读", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  for (let index = 0; index < 6; index++)
    await page.getByRole("button", { name: "放大图谱" }).click();
  await page.waitForTimeout(300);
  await page.screenshot({
    path: "test-results/overview-zoomed.png",
    fullPage: false,
  });
  await page.getByRole("button", { name: "适应画布" }).click();
  await page.getByRole("button", { name: "可信筛选" }).click();
  await page.getByLabel("最低置信度").fill("0.81");
  await expect(page.locator(".confidence-control")).toContainText("81%");
  for (let index = 0; index < 8; index++)
    await page.getByRole("button", { name: "放大图谱" }).click();
  await page.waitForTimeout(300);
  expect(errors).toEqual([]);
  await page.screenshot({
    path: "test-results/confidence-zoomed.png",
    fullPage: false,
  });
});
