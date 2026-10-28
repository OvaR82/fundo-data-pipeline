-- Duplicate prevention at the source. Runs against [fundo]. Idempotent. Additive only:
-- nothing is dropped or rewritten; the original national_id column stays as it is.

-- Fix for the bad schema choice: a bounded, normalized, indexable copy of the national id.
-- '12.345.678-5', '12345678-5', ' 123456785 ' and '012345678-5' all become '123456785'.
-- Rows without a usable id get a per-row value so they never collide with each other.
IF COL_LENGTH('dbo.customers', 'national_id_norm') IS NULL
    ALTER TABLE dbo.customers ADD national_id_norm AS CAST(COALESCE(
        CASE WHEN LEN(UPPER(REPLACE(REPLACE(REPLACE(national_id, N'.', N''), N'-', N''), N' ', N''))) BETWEEN 7 AND 10
             THEN SUBSTRING(UPPER(REPLACE(REPLACE(REPLACE(national_id, N'.', N''), N'-', N''), N' ', N'')),
                            PATINDEX(N'%[^0]%', UPPER(REPLACE(REPLACE(REPLACE(national_id, N'.', N''), N'-', N''), N' ', N''))),
                            10) END,
        N'NOID-' + CAST(customer_id AS NVARCHAR(10))) AS NVARCHAR(20)) PERSISTED;
GO

-- Audit trail of every value merge-source changes: the way back, one UPDATE per row.
IF OBJECT_ID('dbo.customer_merge_log') IS NULL
CREATE TABLE dbo.customer_merge_log (
    log_id       BIGINT IDENTITY(1,1) PRIMARY KEY,
    batch_id     VARCHAR(40)   NOT NULL,
    entity       VARCHAR(20)   NOT NULL,     -- customer | card | advance
    entity_id    BIGINT        NOT NULL,
    column_name  VARCHAR(50)   NOT NULL,
    old_value    NVARCHAR(100) NULL,
    new_value    NVARCHAR(100) NULL,
    logged_at    DATETIME2(3)  NOT NULL DEFAULT SYSUTCDATETIME()
);
GO
