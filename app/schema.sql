-- 展厅换牌应用数据库结构（SQLite）
-- 全部语句可重复执行，应用启动时自动初始化。

CREATE TABLE IF NOT EXISTS plans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    status          TEXT    NOT NULL CHECK (status IN ('active', 'completed')),
    temp_number     INTEGER NOT NULL,
    steps_json      TEXT    NOT NULL,
    next_step_index INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL,
    completed_at    TEXT
);

-- 同一时间只允许一个进行中的换牌方案。
CREATE UNIQUE INDEX IF NOT EXISTS idx_plans_one_active
    ON plans (status) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS exhibits (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id        INTEGER NOT NULL REFERENCES plans (id) ON DELETE CASCADE,
    position       INTEGER NOT NULL,
    explanation    TEXT    NOT NULL,
    initial_number INTEGER NOT NULL,
    target_number  INTEGER NOT NULL,
    UNIQUE (plan_id, position)
);

-- 同一方案内现号、目标号各自不重复。
CREATE UNIQUE INDEX IF NOT EXISTS idx_exhibits_initial
    ON exhibits (plan_id, initial_number);
CREATE UNIQUE INDEX IF NOT EXISTS idx_exhibits_target
    ON exhibits (plan_id, target_number);

-- 牌号位置只由「初始现号 + 已确认指令」推导，不另存冗余状态。
CREATE TABLE IF NOT EXISTS instructions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id         INTEGER NOT NULL REFERENCES plans (id) ON DELETE CASCADE,
    step_index      INTEGER NOT NULL,
    exhibit_id      INTEGER NOT NULL REFERENCES exhibits (id) ON DELETE CASCADE,
    from_number     INTEGER NOT NULL,
    to_number       INTEGER NOT NULL,
    status          TEXT    NOT NULL CHECK (status IN ('pending', 'confirmed', 'withdrawn')),
    observed_number INTEGER,
    created_at      TEXT    NOT NULL,
    finalized_at    TEXT
);

-- 每个方案最多只有一条待核对指令：核对完才能发下一条。
CREATE UNIQUE INDEX IF NOT EXISTS idx_instructions_one_pending
    ON instructions (plan_id) WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_instructions_plan_step
    ON instructions (plan_id, step_index);
CREATE INDEX IF NOT EXISTS idx_instructions_confirmed
    ON instructions (plan_id, exhibit_id) WHERE status = 'confirmed';

-- 上报记录仅作审计，不参与牌号状态计算。
CREATE TABLE IF NOT EXISTS reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    instruction_id  INTEGER NOT NULL REFERENCES instructions (id) ON DELETE CASCADE,
    reported_text   TEXT    NOT NULL,
    reported_number INTEGER,
    result          TEXT    NOT NULL CHECK (result IN (
                        'confirmed', 'withdrawn', 'mismatch', 'invalid',
                        'already_finalized')),
    created_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reports_instruction
    ON reports (instruction_id);
