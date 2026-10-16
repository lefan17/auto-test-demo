# 安全策略

这是一份**自动化测试框架**（自带的 `login_app/` 还故意留有 6 个缺陷用于演示缺陷探针）。
它不部署、不处理真实用户数据，所以这里的"安全"主要指依赖链与测试代码本身。

## 支持的版本

只有 `main` 分支，没有发布版本也没有 git tag。**只有 `main` 的最新提交被维护**，
历史提交与 fork 不接收修复。

## 怎么私下报告

走 GitHub Security Advisories：<https://github.com/lefan17/fullstack-test-framework/security/advisories/new>

**不要开公开 issue**。这个仓库没有安全邮箱，Advisory 是唯一的私下渠道，
它在修复发布前对公众不可见。

## 我们怎么回应

3 个工作日内确认收到；确认后 7 个工作日内给出初步判断（能否复现、影响哪一部分、怎么修）。
修好后要不要在 Advisory 里署名由报告者决定。

## 关于依赖漏洞

`Dependabot`（pip + github-actions，每周一）与 CI 里的 `pip-audit`
（扫 `requirements.lock` 的完整传递依赖树）都会报依赖问题，**两者都不阻塞流水线**：
漏洞库每天都在变，它的红不代表某次提交有问题。**但这些告警要有人看**——
上游披露的 CVE 也值得先开 Advisory 私下说明，而不是等它自己变成公开 issue。
