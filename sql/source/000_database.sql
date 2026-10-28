-- Runs against master. Idempotent.
IF DB_ID('fundo') IS NULL
    CREATE DATABASE fundo;
GO
-- Consistent reads for extraction: CHANGETABLE + base table read in one snapshot.
ALTER DATABASE fundo SET ALLOW_SNAPSHOT_ISOLATION ON;
GO
IF NOT EXISTS (SELECT 1 FROM sys.change_tracking_databases WHERE database_id = DB_ID('fundo'))
    ALTER DATABASE fundo SET CHANGE_TRACKING = ON (CHANGE_RETENTION = 7 DAYS, AUTO_CLEANUP = ON);
GO
