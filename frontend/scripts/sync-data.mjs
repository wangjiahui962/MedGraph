import { copyFile, mkdir } from "node:fs/promises";
const source = new URL("../../data/processed/triples.json", import.meta.url);
const target = new URL("../public/data/", import.meta.url);
try {
  await mkdir(target, { recursive: true });
  await copyFile(source, new URL("triples.json", target));
  console.log("已同步现有三元组 → public/data/triples.json");
} catch (error) {
  console.error(
    "无法同步数据，请确认项目 data/processed/triples.json 存在。",
    error.message,
  );
  process.exitCode = 1;
}
