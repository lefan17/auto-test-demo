# SQL 数据校验练习

> 背景：接口测试里"业务返回 200"不等于"数据正确"。
> 本项目 `testcases/mes/` 已经把这套思路落成了自动化用例（见
> `testcases/mes/data_isolation.py` 与 `conftest.py` 的 `data_isolation_check`），
> 本文是它的**手工版对照练习** —— 先用 SQL 自己想一遍，再看用例是怎么断言的。

## 为什么接口测试要连数据库

客户端看到的是"服务说的"，数据库里才是"实际发生的"。这两者不一致的情况非常多：

- 接口返回 200，但事务回滚了，数据根本没落库；
- 主表写对了，从表和下游没同步（**最容易漏、线上最高发**）；
- 金额字段浮点精度丢了（`99.9` 存成 `99.89999999999999`）；
- 并发下超卖：库存扣了两次，但两次都返回成功。

**这四条里没有一条能靠"看返回码"发现。** 用例里不查库，就等于只测了接口的
"表态"，没测它的"行为"。

## 1. 建表 + 造数

```sql
CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  password TEXT NOT NULL,
  login_count INTEGER DEFAULT 0
);

CREATE TABLE orders (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  amount REAL NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT
);

INSERT INTO users (username, password, login_count) VALUES
  ('admin', '123456', 5),
  ('alice', 'abc123', 0);

INSERT INTO orders (user_id, amount, status, created_at) VALUES
  (1, 99.90, 'paid',   '2025-08-01 10:00:00'),
  (1, 19.50, 'paid',   '2025-08-01 11:30:00'),
  (2, 299.00,'pending','2025-08-02 09:00:00'),
  (1, 5.00,  'cancelled','2025-08-02 12:00:00');
```

> 用 SQLite 是为了零安装。语法与 MySQL 基本通用，只有少数差异
> （比如 `strftime` vs `DATE_FORMAT`），练的是**思路**不是方言。

## 2. 测试场景 → SQL 验证（照着敲一遍）

| 测试场景 | SQL | 验证点 |
|---|---|---|
| 登录后 login_count +1 | `UPDATE users SET login_count = login_count + 1 WHERE username='admin'; SELECT login_count FROM users WHERE username='admin';` | 5 → 6 |
| 下单后订单落库 | `SELECT * FROM orders WHERE user_id=1 ORDER BY created_at DESC;` | 记录存在、金额/状态正确 |
| 用户订单总额统计 | `SELECT u.username, COUNT(o.id) AS cnt, SUM(o.amount) AS total FROM users u LEFT JOIN orders o ON u.id=o.user_id GROUP BY u.id;` | admin: 3 单；alice: 1 单 |
| 只统计已支付订单 | `SELECT COUNT(*), SUM(amount) FROM orders WHERE status='paid';` | 2 单，119.40 |
| 找脏数据（有订单无用户） | `SELECT o.id, o.user_id FROM orders o LEFT JOIN users u ON o.user_id=u.id WHERE u.id IS NULL;` | 应为空（删一个用户后可看到结果） |
| 重复用户名防重 | `INSERT INTO users (username, password) VALUES ('admin', 'x');` | 应报 UNIQUE 约束错误 |

### 两个值得停下来想的地方

**为什么找脏数据必须用 LEFT JOIN 而不是 JOIN？**

`JOIN` 只返回两边都匹配的行 —— 而"订单找不到对应用户"恰恰是**匹配不上的那一行**，
用 `JOIN` 会把这些行过滤掉，查询结果永远是空的，看起来"没有脏数据"。
**LEFT JOIN + WHERE 右表 IS NULL** 才能把"孤儿行"捞出来。

这个坑在真实项目里很常见：用一个永远返回空的查询来"证明"数据干净。

**金额为什么建议用整数分而不是 REAL？**

`99.90` 在 IEEE 754 里存不下精确值。测试里写
`assert abs(sum - 119.40) < 1e-9` 是在**掩盖问题**而不是解决它。
正确做法是在库里用整数存"分"，接口层做转换 ——
本项目 MES 那边就是这么做的，见 `mock_server/quantity.py`。

## 3. 从手工 SQL 到自动化断言

上面这些查询在真实用例里的形态是：

```python
def test_issue_never_oversells(db):
    """并发领料后，库存可用量的合计必须等于预期 —— 账实相符。"""
    before = query_one("SELECT SUM(available_qty) AS s FROM stock")
    ...
    after = query_one("SELECT SUM(available_qty) AS s FROM stock")
    assert after["s"] == before["s"] - expected_issue
```

三个要点，缺一个断言就不可靠：

1. **查库必须只读** —— 生产/预发环境的自动化用例绝不能写库。
   本项目 `common/` 里的查询封装只提供 `query_one` / `query_all`，没有写接口。
2. **异步场景不能用固定 sleep** —— 用轮询等待条件成立（见 MES 用例里的等待辅助）。
3. **不能只断言"数量没变"** —— 领料只 `UPDATE` 一行，行数完全不变但账实已经不符。
   所以要**统计业务量（库存合计）而不只是行数**，这正是
   `testcases/mes/data_isolation.py` 同时统计两个指标的原因。

## 4. 复盘问题（面试常问）

1. 接口返回 200 就代表数据写对了吗？还需要验证什么？（→ 查库）
2. 为什么要用 `LEFT JOIN` 而不是 `JOIN` 找脏数据？
3. `GROUP BY` 时如何过滤聚合结果？（→ `HAVING`，因为 `WHERE` 在聚合前执行）
4. `WHERE` 和 `HAVING` 的执行顺序是什么？为什么不能在 `WHERE` 里用聚合函数？
5. 金额字段用浮点存会有什么问题？你们项目是怎么处理的？
