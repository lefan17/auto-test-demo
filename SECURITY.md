# 安全策略

这是**自动化测试框架**（`login_app/` 还故意留有 6 个缺陷供缺陷探针演示），不部署、不处理真实
用户数据，所以这里的"安全"主要指依赖链与测试代码本身。

## 支持的版本

只维护 `main` 的最新提交；历史提交与 fork 不接收修复。没有发布版本（`gh release list` 为空），
远端也没有任何 tag（本地那个 `pre-regroup` 只是本地标记，不代表版本）。

## 怎么私下报告

走 GitHub Security Advisories：<https://github.com/lefan17/fullstack-test-framework/security/advisories/new>
**不要开公开 issue**——本仓库没有安全邮箱，Advisory 是唯一渠道，修复发布前对公众不可见。

## 我们怎么回应

会尽快确认收到；确认后给出初步判断（能否复现、影响哪一部分、怎么修）并同步修复进度。
修好后是否署名由报告者决定。依赖漏洞同理：`Dependabot` 与 CI 的 `pip-audit`（扫
`requirements.lock`）都不阻塞流水线，但红了要有人看，上游披露的 CVE 值得先开 Advisory。
