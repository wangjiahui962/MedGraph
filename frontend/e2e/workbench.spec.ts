import { test, expect } from "@playwright/test";

test("真实数据：查询、证据、相邻实体、筛选、恢复", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(page.locator(".stat").nth(2)).toContainText("857");
  await expect(page.locator(".graph-canvas canvas").first()).toBeVisible();
  await page.getByRole("textbox", { name: "搜索实体名称" }).fill("猪瘟");
  await page.getByRole("button", { name: "猪瘟 疾病", exact: true }).click();
  await expect(page.locator(".entity-name")).toHaveText("猪瘟");
  await page.locator(".relation-link").filter({ hasText: "常见症状" }).click();
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
    .getByRole("checkbox", { name: "症状", exact: false })
    .first()
    .uncheck();
  await page.getByRole("textbox", { name: "搜索实体名称" }).fill("腹泻");
  await expect(page.locator(".results")).toContainText("没有匹配实体");
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
  await page.getByRole("textbox", { name: "搜索实体名称" }).fill("2020年天津");
  await page.locator(".entity-result").first().click();
  await expect(page.locator(".entity-name")).toContainText("2020年天津");
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
