-- Source schema, shaped like the operational database. Runs against [fundo]. Idempotent.

IF OBJECT_ID('dbo.card_brands') IS NULL
CREATE TABLE dbo.card_brands (
    brand_code  VARCHAR(10)   NOT NULL PRIMARY KEY,
    brand_name  NVARCHAR(50)  NOT NULL
);
GO

IF OBJECT_ID('dbo.customers') IS NULL
CREATE TABLE dbo.customers (
    customer_id  INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    -- BAD SCHEMA CHOICE (deliberate, mirrors production): the national identifier (Chilean RUT)
    -- is unbounded free text with no format and no uniqueness. It cannot be indexed, and the
    -- same person arrives as '12.345.678-5', '12345678-5' or ' 123456785 ' -> duplicates.
    national_id  NVARCHAR(MAX) NULL,
    first_name   NVARCHAR(100) NOT NULL,
    last_name    NVARCHAR(100) NOT NULL,
    email        NVARCHAR(320) NULL,
    phone        NVARCHAR(50)  NULL,
    birth_date   DATE          NULL,
    created_at   DATETIME2(3)  NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at   DATETIME2(3)  NOT NULL DEFAULT SYSUTCDATETIME(),
    -- Bookkeeping for duplicate resolution (filled by merge-source; NULL = active customer).
    merged_into_customer_id INT NULL CONSTRAINT fk_customers_merged_into REFERENCES dbo.customers(customer_id),
    dedup_status VARCHAR(20) NULL CONSTRAINT ck_customers_dedup_status
        CHECK (dedup_status IN ('merged', 'test', 'review_hold'))
);
GO

IF OBJECT_ID('dbo.advances') IS NULL
CREATE TABLE dbo.advances (
    advance_id   INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    customer_id  INT NOT NULL REFERENCES dbo.customers(customer_id),
    amount       DECIMAL(12,2) NOT NULL,
    status       VARCHAR(20)   NOT NULL
        CHECK (status IN ('requested','approved','funded','paid_off','rejected')),
    created_at   DATETIME2(3)  NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at   DATETIME2(3)  NOT NULL DEFAULT SYSUTCDATETIME()
);
GO

IF OBJECT_ID('dbo.cards') IS NULL
CREATE TABLE dbo.cards (
    card_id      INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    customer_id  INT NOT NULL REFERENCES dbo.customers(customer_id),
    brand_code   VARCHAR(10) NOT NULL REFERENCES dbo.card_brands(brand_code),
    last4        CHAR(4)     NOT NULL,
    status       VARCHAR(10) NOT NULL CHECK (status IN ('active','blocked','expired')),
    created_at   DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at   DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME()
);
GO

IF OBJECT_ID('dbo.transactions') IS NULL
CREATE TABLE dbo.transactions (
    transaction_id  BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    card_id         INT NOT NULL REFERENCES dbo.cards(card_id),
    amount          DECIMAL(12,2) NOT NULL,
    merchant        NVARCHAR(100) NOT NULL,
    txn_at          DATETIME2(3)  NOT NULL,
    created_at      DATETIME2(3)  NOT NULL DEFAULT SYSUTCDATETIME()
);
GO

-- Append-only version table: one row per insert/update of a customer, written by a trigger.
IF OBJECT_ID('dbo.customer_history') IS NULL
CREATE TABLE dbo.customer_history (
    history_id   BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    customer_id  INT          NOT NULL,
    operation    CHAR(1)      NOT NULL,       -- I = insert, U = update
    national_id  NVARCHAR(MAX) NULL,
    first_name   NVARCHAR(100) NULL,
    last_name    NVARCHAR(100) NULL,
    email        NVARCHAR(320) NULL,
    phone        NVARCHAR(50)  NULL,
    changed_at   DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME()
);
GO

CREATE OR ALTER TRIGGER dbo.trg_customers_history ON dbo.customers
AFTER INSERT, UPDATE AS
BEGIN
    SET NOCOUNT ON;
    INSERT dbo.customer_history (customer_id, operation, national_id, first_name, last_name, email, phone)
    SELECT i.customer_id,
           CASE WHEN EXISTS (SELECT 1 FROM deleted) THEN 'U' ELSE 'I' END,
           i.national_id, i.first_name, i.last_name, i.email, i.phone
    FROM inserted i;
END;
GO

-- Scratch table nobody owns. Seeded once, never read by any process.
IF OBJECT_ID('dbo.tmp_marketing_export_2021') IS NULL
CREATE TABLE dbo.tmp_marketing_export_2021 (
    row_id     INT IDENTITY(1,1) PRIMARY KEY,
    email      NVARCHAR(MAX),
    campaign   NVARCHAR(MAX),
    exported   DATETIME2(3) DEFAULT SYSUTCDATETIME()
);
GO

-- Change Tracking on every mutable table we replicate incrementally.
-- Not on customer_history (insert-only, loaded by watermark) nor card_brands (full snapshot).
IF NOT EXISTS (SELECT 1 FROM sys.change_tracking_tables WHERE object_id = OBJECT_ID('dbo.customers'))
    ALTER TABLE dbo.customers ENABLE CHANGE_TRACKING;
IF NOT EXISTS (SELECT 1 FROM sys.change_tracking_tables WHERE object_id = OBJECT_ID('dbo.advances'))
    ALTER TABLE dbo.advances ENABLE CHANGE_TRACKING;
IF NOT EXISTS (SELECT 1 FROM sys.change_tracking_tables WHERE object_id = OBJECT_ID('dbo.cards'))
    ALTER TABLE dbo.cards ENABLE CHANGE_TRACKING;
IF NOT EXISTS (SELECT 1 FROM sys.change_tracking_tables WHERE object_id = OBJECT_ID('dbo.transactions'))
    ALTER TABLE dbo.transactions ENABLE CHANGE_TRACKING;
GO
