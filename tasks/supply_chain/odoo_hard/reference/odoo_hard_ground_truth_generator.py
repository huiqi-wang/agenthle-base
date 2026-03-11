"""odoo_hard_ground_truth_generator.py

PURPOSE
  Create a "ground truth" database state for the odoo_hard task WITHOUT using the Odoo UI,
  by inserting/updating ONLY the records that the current judger (main.py) reads from Postgres.

  This is DB-level spoofing for convenience/testing.

USAGE (Windows / PowerShell)
  $env:PGPASSWORD="openpgpwd"

  # If DB missing, auto-create from TEMPLATE=AgentService
  python odoo_hard_ground_truth_generator.py --db odoo_hard --self-check

  # Recreate DB from template each time
  python odoo_hard_ground_truth_generator.py --db odoo_hard --recreate-db --ensure-db-template AgentService --self-check
"""

import argparse
import os
import subprocess
import sys
import json


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = "5432"
DEFAULT_USER = "openpg"
DEFAULT_PASSWORD = "openpgpwd"
DEFAULT_DB = "odoo_hard"
DEFAULT_TAG = "odoo_hard"

PSQL = "psql"




def run_psql(
    sql: str,
    host: str,
    port: int,
    user: str,
    db: str,
    password: str | None = None,
    on_error_stop: bool = True,
) -> str:
    """Run a SQL script against Postgres using psql and return stdout.

    NOTE (Windows): Passing a very large SQL string via `-c` can hit WinError 206
    (command line too long). To make this robust, we always write the SQL into a
    temporary .sql file and execute via `psql -f <file>`.
    """
    env = os.environ.copy()
    if password:
        env["PGPASSWORD"] = password

    import tempfile
    fd, tmp_path = tempfile.mkstemp(prefix="odoo_hard_", suffix=".sql")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(sql)
            if not sql.endswith("\n"):
                f.write("\n")

        cmd = [
            "psql",
            "-h", host,
            "-p", str(port),
            "-U", user,
            "-d", db,
            "-q",
            "-X",
            "-t",
            "-A",
            "-f", tmp_path,
        ]
        if on_error_stop:
            cmd.extend(["-v", "ON_ERROR_STOP=1"])

        p = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(
                f"psql failed (exit={p.returncode})\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}"
            )
        return p.stdout
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", default=DEFAULT_PORT)
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--password", default=DEFAULT_PASSWORD)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--tag", default=DEFAULT_TAG)
    ap.add_argument("--ensure-db-template", default="AgentService",
                    help="If target DB does not exist, create it using this TEMPLATE database (default: AgentService). Set empty to create without template.")
    ap.add_argument("--recreate-db", action="store_true",
                    help="Drop and recreate the DB before inserting ground truth (uses --ensure-db-template).")
    ap.add_argument("--self-check", action="store_true",
                    help="Run the same evidence query as judger and print JSON.")
    args = ap.parse_args()

    tag = args.tag.replace("'", "''")

    # ---- 0) Ensure DB exists (optionally recreate) ----
    def db_exists(name: str) -> bool:
        safe = name.replace("'", "''")
        q = f"SELECT 1 FROM pg_database WHERE datname='{safe}' LIMIT 1;"
        out = run_psql(q, host=args.host, port=args.port, user=args.user, db="postgres", password=args.password, on_error_stop=True)
        return out.strip() == "1"

    def terminate_db_sessions(name: str):
        if not name:
            return
        safe = name.replace("'", "''")
        sql = f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{safe}' AND pid<>pg_backend_pid();"
        try:
            run_psql(sql, host=args.host, port=args.port, user=args.user, db="postgres", password=args.password, on_error_stop=False)
        except Exception:
            pass

    def drop_db(name: str):
        terminate_db_sessions(name)
        safe = name.replace('"', '""')
        run_psql(f'DROP DATABASE IF EXISTS "{safe}";', host=args.host, port=args.port, user=args.user, db="postgres", password=args.password, on_error_stop=True)

    def create_db(name: str, template: str | None):
        safe = name.replace('"', '""')
        if template:
            tmpl = template.replace('"', '""')
            terminate_db_sessions(template)
            run_psql(f'CREATE DATABASE "{safe}" WITH TEMPLATE "{tmpl}" OWNER {args.user};',
                     host=args.host, port=args.port, user=args.user, db="postgres", password=args.password, on_error_stop=True)
        else:
            run_psql(f'CREATE DATABASE "{safe}" OWNER {args.user};',
                     host=args.host, port=args.port, user=args.user, db="postgres", password=args.password, on_error_stop=True)

    template = (args.ensure_db_template or "").strip()
    template = template if template else None

    if args.recreate_db:
        print(f"[0/2] Recreating database: {args.db}")
        drop_db(args.db)
        if template:
            print(f"      using TEMPLATE: {template}")
        create_db(args.db, template)
    else:
        if not db_exists(args.db):
            print(f"[0/2] Database '{args.db}' does not exist. Creating it now...")
            if template:
                print(f"      using TEMPLATE: {template}")
            create_db(args.db, template)

    # ---- 1) Build minimal ground-truth objects (schema-adaptive) ----
    build_sql = f"""
CREATE OR REPLACE FUNCTION odoo_hard_has_col(p_table text, p_col text)
RETURNS boolean
LANGUAGE sql
AS $$
  SELECT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='public' AND table_name=p_table AND column_name=p_col
  );
$$;

CREATE OR REPLACE FUNCTION odoo_hard_text_sql(p_table text, p_col text, p_text text)
RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
  dt text;
BEGIN
  SELECT data_type INTO dt
  FROM information_schema.columns
  WHERE table_schema='public' AND table_name=p_table AND column_name=p_col
  LIMIT 1;

  IF dt = 'jsonb' THEN
    RETURN format('jsonb_build_object(''en_US'', %L)', p_text);
  ELSIF dt = 'json' THEN
    RETURN format('json_build_object(''en_US'', %L)', p_text);
  ELSE
    RETURN format('%L', p_text);
  END IF;
END;
$$;

CREATE OR REPLACE FUNCTION odoo_hard_default_sql(
  p_table text,
  p_col text,
  p_uid int,
  p_company int,
  p_uom int,
  p_categ int,
  p_tracking text
)
RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
  dt text;
  udt text;
  ref_table text;
BEGIN
  SELECT data_type, udt_name INTO dt, udt
  FROM information_schema.columns
  WHERE table_schema='public' AND table_name=p_table AND column_name=p_col
  LIMIT 1;

  -- Common FK / required refs
  IF p_col IN ('create_uid','write_uid') THEN
    RETURN format('%s', p_uid);
  ELSIF p_col = 'company_id' THEN
    RETURN format('%s', p_company);
  ELSIF p_col IN ('categ_id') THEN
    RETURN format('%s', p_categ);
  ELSIF p_col IN ('uom_id','uom_po_id','product_uom_id','uom_uom_id','base_unit_id') THEN
    RETURN format('%s', p_uom);
  ELSIF p_col IN ('tracking') THEN
    RETURN format('%L', COALESCE(p_tracking,'none'));
  ELSIF p_col IN ('service_tracking') THEN
    RETURN format('%L', 'no');
  ELSIF p_col IN ('service_type') THEN
    RETURN format('%L', 'manual');
  ELSIF p_col IN ('type','detailed_type') THEN
    RETURN format('%L', 'product');
  ELSIF p_col IN ('publish_date') THEN
    RETURN 'now()';
  ELSIF p_col IN ('base_unit_count') THEN
    RETURN '1';

ELSIF p_col IN ('auto_post') THEN
  -- account_move.auto_post is a required selection in some schemas
  RETURN format('%L', 'no');
ELSIF p_col IN ('plan_id') THEN
  -- Analytic Plans (Odoo 17+): analytic accounts often require plan_id
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='account_analytic_plan') THEN
    RETURN '(SELECT id FROM account_analytic_plan ORDER BY id LIMIT 1)';
  ELSE
    RETURN '1';
  END IF;
  ELSIF p_col = 'active' THEN
    RETURN 'true';
  END IF;

  -- Website-related ids sometimes appear in customized schemas
  IF p_col = 'website_id' THEN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='website') THEN
      RETURN '(SELECT id FROM website ORDER BY id LIMIT 1)';
    ELSE
      RETURN '1';
    END IF;
  END IF;

  -- By type

-- Sales/Purchase common foreign keys
IF p_col IN ('partner_id','partner_invoice_id','partner_shipping_id') THEN
  RETURN '(SELECT id FROM res_partner ORDER BY id LIMIT 1)';
ELSIF p_col = 'pricelist_id' THEN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='product_pricelist') THEN
    RETURN '(SELECT id FROM product_pricelist ORDER BY id LIMIT 1)';
  ELSE
    RETURN '1';
  END IF;
ELSIF p_col = 'warehouse_id' THEN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='stock_warehouse') THEN
    RETURN '(SELECT id FROM stock_warehouse ORDER BY id LIMIT 1)';
  ELSE
    RETURN '1';
  END IF;
ELSIF p_col = 'currency_id' THEN
  RETURN format('(SELECT currency_id FROM res_company WHERE id=%s)', p_company);
ELSIF p_col IN ('user_id','salesman_id') THEN
  RETURN format('%s', p_uid);
ELSIF p_col IN ('team_id') THEN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='crm_team') THEN
    RETURN '(SELECT id FROM crm_team ORDER BY id LIMIT 1)';
  ELSE
    RETURN '1';
  END IF;
ELSIF p_col IN ('payment_term_id') THEN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='account_payment_term') THEN
    RETURN '(SELECT id FROM account_payment_term ORDER BY id LIMIT 1)';
  ELSE
    RETURN '1';
  END IF;
END IF;

-- Generic FK fallback: for *_id columns, if a FK constraint exists, pick any existing referenced id
IF dt IN ('integer','bigint','smallint') AND right(p_col, 3) = '_id' THEN
  SELECT ccu.table_name INTO ref_table
  FROM information_schema.table_constraints tc
  JOIN information_schema.key_column_usage kcu
    ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
  JOIN information_schema.constraint_column_usage ccu
    ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
  WHERE tc.constraint_type = 'FOREIGN KEY'
    AND tc.table_schema = 'public'
    AND tc.table_name = p_table
    AND kcu.column_name = p_col
  LIMIT 1;

  IF ref_table IS NOT NULL THEN
    RETURN format('(SELECT id FROM %I ORDER BY id LIMIT 1)', ref_table);
  END IF;
END IF;
  -- By type
  IF dt IN ('integer','bigint','smallint') THEN
    RETURN '1';
  ELSIF dt IN ('numeric','double precision','real') THEN
    RETURN '0';
  ELSIF dt = 'boolean' THEN
    RETURN 'false';
  ELSIF dt LIKE 'timestamp%' THEN
    RETURN 'now()';
  ELSIF dt = 'date' THEN
    RETURN 'current_date';
  ELSIF dt = 'jsonb' THEN
    RETURN format('jsonb_build_object(''en_US'', %L)', '');
  ELSIF dt = 'json' THEN
    RETURN format('json_build_object(''en_US'', %L)', '');
  ELSE
    -- text/char/varchar/others: safest empty string
    RETURN quote_literal('');
  END IF;
END;
$$;

CREATE OR REPLACE FUNCTION odoo_hard_ensure_product(
  p_code text,
  p_name text,
  p_tracking text
)
RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
  v_prod int;
  v_tmpl int;

  v_uid int;
  v_company int;
  v_uom int;
  v_categ int;

  cols text;
  vals text;
  sql text;
  used_cols text[];
  rec record;
  rec2 record;

BEGIN
  SELECT pp.id INTO v_prod
  FROM product_product pp
  JOIN product_template pt ON pt.id = pp.product_tmpl_id
  WHERE pt.default_code = p_code
  ORDER BY pp.id
  LIMIT 1;

  IF v_prod IS NOT NULL THEN
    RETURN v_prod;
  END IF;

  SELECT COALESCE((SELECT id FROM res_users WHERE login='admin' ORDER BY id LIMIT 1), 1) INTO v_uid;
  SELECT COALESCE((SELECT id FROM res_company ORDER BY id LIMIT 1), 1) INTO v_company;
  SELECT (SELECT id FROM uom_uom ORDER BY id LIMIT 1) INTO v_uom;
  SELECT (SELECT id FROM product_category ORDER BY id LIMIT 1) INTO v_categ;

  -- -------- product_template (dynamic insert) --------
  cols := 'name, default_code, create_uid, write_uid, create_date, write_date';
  vals := format('%s, %L, %s, %s, now(), now()', odoo_hard_text_sql('product_template','name',p_name), p_code, v_uid, v_uid);
  used_cols := ARRAY['name','default_code','create_uid','write_uid','create_date','write_date'];

  -- Optional-but-common columns we want to set explicitly if present
  IF odoo_hard_has_col('product_template','company_id') THEN
    cols := cols || ', company_id'; vals := vals || format(', %s', v_company);
    used_cols := array_append(used_cols,'company_id');
  END IF;

  IF odoo_hard_has_col('product_template','categ_id') THEN
    cols := cols || ', categ_id'; vals := vals || format(', %s', v_categ);
    used_cols := array_append(used_cols,'categ_id');
  END IF;

  IF odoo_hard_has_col('product_template','uom_id') THEN
    cols := cols || ', uom_id'; vals := vals || format(', %s', v_uom);
    used_cols := array_append(used_cols,'uom_id');
  END IF;

  IF odoo_hard_has_col('product_template','uom_po_id') THEN
    cols := cols || ', uom_po_id'; vals := vals || format(', %s', v_uom);
    used_cols := array_append(used_cols,'uom_po_id');
  END IF;

  IF odoo_hard_has_col('product_template','sale_ok') THEN
    cols := cols || ', sale_ok'; vals := vals || ', true';
    used_cols := array_append(used_cols,'sale_ok');
  END IF;

  IF odoo_hard_has_col('product_template','purchase_ok') THEN
    cols := cols || ', purchase_ok'; vals := vals || ', true';
    used_cols := array_append(used_cols,'purchase_ok');
  END IF;

  IF odoo_hard_has_col('product_template','tracking') THEN
    cols := cols || ', tracking'; vals := vals || format(', %L', p_tracking);
    used_cols := array_append(used_cols,'tracking');
  END IF;

  IF odoo_hard_has_col('product_template','service_tracking') THEN
    cols := cols || ', service_tracking'; vals := vals || format(', %L', 'no');
    used_cols := array_append(used_cols,'service_tracking');
  END IF;

  IF odoo_hard_has_col('product_template','service_type') THEN
    cols := cols || ', service_type'; vals := vals || format(', %L', 'manual');
    used_cols := array_append(used_cols,'service_type');
  END IF;

  IF odoo_hard_has_col('product_template','publish_date') THEN
    cols := cols || ', publish_date'; vals := vals || ', now()';
    used_cols := array_append(used_cols,'publish_date');
  END IF;

  IF odoo_hard_has_col('product_template','base_unit_count') THEN
    cols := cols || ', base_unit_count'; vals := vals || ', 1';
    used_cols := array_append(used_cols,'base_unit_count');
  END IF;

  IF odoo_hard_has_col('product_template','detailed_type') THEN
    cols := cols || ', detailed_type'; vals := vals || format(', %L', 'product');
    used_cols := array_append(used_cols,'detailed_type');
  ELSIF odoo_hard_has_col('product_template','type') THEN
    cols := cols || ', type'; vals := vals || format(', %L', 'product');
    used_cols := array_append(used_cols,'type');
  END IF;

  -- AUTO-FILL: add any remaining NOT NULL columns without defaults
  FOR rec IN
    SELECT column_name
    FROM information_schema.columns
    WHERE table_schema='public' AND table_name='product_template'
      AND is_nullable='NO'
      AND column_default IS NULL
      AND column_name <> 'id'
      AND NOT (column_name = ANY(used_cols))
    ORDER BY ordinal_position
  LOOP
    cols := cols || ', ' || quote_ident(rec.column_name);
    vals := vals || ', ' || odoo_hard_default_sql('product_template', rec.column_name, v_uid, v_company, v_uom, v_categ, p_tracking);
    used_cols := array_append(used_cols, rec.column_name::text);
  END LOOP;

  sql := format('INSERT INTO product_template (%s) VALUES (%s) RETURNING id', cols, vals);
  EXECUTE sql INTO v_tmpl;

  -- -------- product_product (dynamic insert) --------
  cols := 'product_tmpl_id';
  vals := format('%s', v_tmpl);
  used_cols := ARRAY['product_tmpl_id'];

  IF odoo_hard_has_col('product_product','create_uid') THEN
    cols := cols || ', create_uid'; vals := vals || format(', %s', v_uid);
    used_cols := array_append(used_cols,'create_uid');
  END IF;
  IF odoo_hard_has_col('product_product','write_uid') THEN
    cols := cols || ', write_uid'; vals := vals || format(', %s', v_uid);
    used_cols := array_append(used_cols,'write_uid');
  END IF;
  IF odoo_hard_has_col('product_product','create_date') THEN
    cols := cols || ', create_date'; vals := vals || ', now()';
    used_cols := array_append(used_cols,'create_date');
  END IF;
  IF odoo_hard_has_col('product_product','write_date') THEN
    cols := cols || ', write_date'; vals := vals || ', now()';
    used_cols := array_append(used_cols,'write_date');
  END IF;
  IF odoo_hard_has_col('product_product','active') THEN
    cols := cols || ', active'; vals := vals || ', true';
    used_cols := array_append(used_cols,'active');
  END IF;

  IF odoo_hard_has_col('product_product','base_unit_count') THEN
    cols := cols || ', base_unit_count'; vals := vals || ', 1';
    used_cols := array_append(used_cols,'base_unit_count');
  END IF;

  -- AUTO-FILL: add any remaining NOT NULL columns without defaults
  FOR rec IN
    SELECT column_name
    FROM information_schema.columns
    WHERE table_schema='public' AND table_name='product_product'
      AND is_nullable='NO'
      AND column_default IS NULL
      AND column_name <> 'id'
      AND NOT (column_name = ANY(used_cols))
    ORDER BY ordinal_position
  LOOP
    cols := cols || ', ' || quote_ident(rec.column_name);
    vals := vals || ', ' || odoo_hard_default_sql('product_product', rec.column_name, v_uid, v_company, v_uom, v_categ, p_tracking);
    used_cols := array_append(used_cols, rec.column_name::text);
  END LOOP;

  sql := format('INSERT INTO product_product (%s) VALUES (%s) RETURNING id', cols, vals);
  EXECUTE sql INTO v_prod;

  RETURN v_prod;
END;
$$;

CREATE OR REPLACE FUNCTION odoo_hard_insert_sale_order_line(
  p_order_id int,
  p_product_id int,
  p_qty numeric,
  p_uom int,
  p_price numeric,
  p_desc text
)
RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
  v_id int;
  v_uid int;
  v_company int;
  cols text;
  vals text;
  sql text;
  used_cols text[];
  rec record;
BEGIN
  -- Find existing similar line
  SELECT sol.id INTO v_id
  FROM sale_order_line sol
  WHERE sol.order_id = p_order_id
    AND sol.product_id = p_product_id
  ORDER BY sol.id DESC
  LIMIT 1;

  IF v_id IS NOT NULL THEN
    RETURN v_id;
  END IF;

  SELECT COALESCE((SELECT id FROM res_users WHERE login='admin' ORDER BY id LIMIT 1), 1) INTO v_uid;
  SELECT COALESCE((SELECT id FROM res_company ORDER BY id LIMIT 1), 1) INTO v_company;

  cols := 'order_id, product_id';
  vals := format('%s, %s', p_order_id, p_product_id);
  used_cols := ARRAY['order_id','product_id'];

  -- qty
  IF odoo_hard_has_col('sale_order_line','product_uom_qty') THEN
    cols := cols || ', product_uom_qty';
    vals := vals || format(', %s', p_qty);
    used_cols := array_append(used_cols,'product_uom_qty');
  END IF;

  -- uom column varies by version/customization
  IF odoo_hard_has_col('sale_order_line','product_uom_id') THEN
    cols := cols || ', product_uom_id';
    vals := vals || format(', %s', p_uom);
    used_cols := array_append(used_cols,'product_uom_id');
  ELSIF odoo_hard_has_col('sale_order_line','product_uom') THEN
    cols := cols || ', product_uom';
    vals := vals || format(', %s', p_uom);
    used_cols := array_append(used_cols,'product_uom');
  END IF;

  -- price
  IF odoo_hard_has_col('sale_order_line','price_unit') THEN
    cols := cols || ', price_unit';
    vals := vals || format(', %s', p_price);
    used_cols := array_append(used_cols,'price_unit');
  END IF;

  -- description/name (may be json/jsonb)
  IF odoo_hard_has_col('sale_order_line','name') THEN
    cols := cols || ', name';
    vals := vals || format(', %s', odoo_hard_text_sql('sale_order_line','name',p_desc));
    used_cols := array_append(used_cols,'name');
  END IF;

  -- bookkeeping/audit
  IF odoo_hard_has_col('sale_order_line','company_id') THEN
    cols := cols || ', company_id';
    vals := vals || format(', %s', v_company);
    used_cols := array_append(used_cols,'company_id');
  END IF;
  IF odoo_hard_has_col('sale_order_line','create_uid') THEN
    cols := cols || ', create_uid';
    vals := vals || format(', %s', v_uid);
    used_cols := array_append(used_cols,'create_uid');
  END IF;
  IF odoo_hard_has_col('sale_order_line','write_uid') THEN
    cols := cols || ', write_uid';
    vals := vals || format(', %s', v_uid);
    used_cols := array_append(used_cols,'write_uid');
  END IF;
  IF odoo_hard_has_col('sale_order_line','create_date') THEN
    cols := cols || ', create_date';
    vals := vals || ', now()';
    used_cols := array_append(used_cols,'create_date');
  END IF;
  IF odoo_hard_has_col('sale_order_line','write_date') THEN
    cols := cols || ', write_date';
    vals := vals || ', now()';
    used_cols := array_append(used_cols,'write_date');
  END IF;

  -- AUTO-FILL any remaining NOT NULL without default
  FOR rec IN
    SELECT column_name
    FROM information_schema.columns
    WHERE table_schema='public' AND table_name='sale_order_line'
      AND is_nullable='NO'
      AND column_default IS NULL
      AND column_name <> 'id'
      AND NOT (column_name = ANY(used_cols))
    ORDER BY ordinal_position
  LOOP
    cols := cols || ', ' || quote_ident(rec.column_name);
    vals := vals || ', ' || odoo_hard_default_sql('sale_order_line', rec.column_name::text, v_uid, v_company, p_uom, 1, 'none');
    used_cols := array_append(used_cols, rec.column_name::text);
  END LOOP;

  sql := format('INSERT INTO sale_order_line (%s) VALUES (%s) RETURNING id', cols, vals);
  EXECUTE sql INTO v_id;
  RETURN v_id;
END;
$$;
-- Helper: robust insert for account_move (invoices/bills/credit notes) across Odoo versions/customizations
CREATE OR REPLACE FUNCTION odoo_hard_upsert_account_move(
  p_name text,
  p_move_type text,
  p_state text,
  p_payment_state text,
  p_company_id integer,
  p_journal_id integer,
  p_currency_id integer,
  p_ref text,
  p_amount_total numeric,
  p_uid integer,
  p_uom integer
) RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
  v_id integer;
  v_partner integer;
  cols text;
  vals text;
  sql text;
  used_cols text[];
  rec record;
BEGIN
  SELECT id INTO v_id
  FROM account_move
  WHERE name = p_name AND move_type = p_move_type
  ORDER BY id DESC
  LIMIT 1;

  IF v_id IS NOT NULL THEN
    RETURN v_id;
  END IF;

  SELECT id INTO v_partner FROM res_partner ORDER BY id LIMIT 1;

  cols := 'name, move_type, state, payment_state, company_id, journal_id, currency_id, date, ref, amount_total, create_uid, write_uid, create_date, write_date';
  vals := format('%s, %L, %L, %L, %s, %s, %s, current_date, %s, %s, %s, %s, now(), now()',
                 odoo_hard_text_sql('account_move','name', p_name),
                 p_move_type, p_state, p_payment_state,
                 p_company_id, p_journal_id, p_currency_id,
                 odoo_hard_text_sql('account_move','ref', p_ref),
                 p_amount_total, p_uid, p_uid);

  used_cols := ARRAY['name','move_type','state','payment_state','company_id','journal_id','currency_id','date','ref','amount_total','create_uid','write_uid','create_date','write_date'];

  -- Required in some schemas (NOT NULL)
  IF odoo_hard_has_col('account_move','auto_post') THEN
    cols := cols || ', auto_post';
    vals := vals || format(', %L', 'no');
    used_cols := array_append(used_cols,'auto_post');
  END IF;

  -- Invoices sometimes require invoice_date
  IF odoo_hard_has_col('account_move','invoice_date') THEN
    cols := cols || ', invoice_date';
    vals := vals || ', current_date';
    used_cols := array_append(used_cols,'invoice_date');
  END IF;

  -- Partner fields (customizations may add NOT NULL)
  IF odoo_hard_has_col('account_move','partner_id') THEN
    cols := cols || ', partner_id';
    vals := vals || format(', %s', v_partner);
    used_cols := array_append(used_cols,'partner_id');
  END IF;
  IF odoo_hard_has_col('account_move','partner_shipping_id') THEN
    cols := cols || ', partner_shipping_id';
    vals := vals || format(', %s', v_partner);
    used_cols := array_append(used_cols,'partner_shipping_id');
  END IF;
  IF odoo_hard_has_col('account_move','partner_invoice_id') THEN
    cols := cols || ', partner_invoice_id';
    vals := vals || format(', %s', v_partner);
    used_cols := array_append(used_cols,'partner_invoice_id');
  END IF;

  -- Fill remaining NOT NULL columns without defaults
  FOR rec IN
    SELECT column_name
    FROM information_schema.columns
    WHERE table_schema='public' AND table_name='account_move'
      AND is_nullable='NO'
      AND column_default IS NULL
      AND column_name <> 'id'
      AND NOT (column_name = ANY(used_cols))
    ORDER BY ordinal_position
  LOOP
    cols := cols || ', ' || quote_ident(rec.column_name);
    vals := vals || ', ' || odoo_hard_default_sql('account_move', rec.column_name::text, p_uid, p_company_id, p_uom, 1, 'none');
    used_cols := array_append(used_cols, rec.column_name::text);
  END LOOP;

  sql := format('INSERT INTO account_move (%s) VALUES (%s) RETURNING id', cols, vals);
  EXECUTE sql INTO v_id;
  RETURN v_id;
END;
$$;


DO $$
DECLARE
  v_uid int;
  v_company int;
  v_uom int;

  v_cur_eur int;
  v_j_sale int;
  v_j_purchase int;

  v_loc_wh int;
  v_loc_my int;

  p_fp int; p_sa int; p_sc int;
  p_rm010 int; p_rm020 int; p_rm030 int; p_rm040 int;
  p_acc int;

  lot_ssk1 int; lot_ssk2 int; lot_ssk3 int;
  lot_cs1 int; lot_cs2 int; lot_cs3 int;

  v_so int;
  v_po int;

  v_picktype_drop int;
  v_pick_drop int;
  v_pick_sc int;

  v_mo_sa int; v_mo_fp int;
  v_wc int;
  v_wo1 int; v_wo2 int;

  v_scrap int;

  v_lc_bill int;
  v_lc int;
  v_an_plan int;


  -- dynamic insert helpers
  cols text;
  vals text;
  sql text;
  used_cols text[];
  rec record;
  rec2 record;
  v_partner int;
  v_pricelist int;
  v_sml_qty_col text;
  v_sml_uom_col text;
  v_exists boolean;

BEGIN
  SELECT COALESCE((SELECT id FROM res_users WHERE login='admin' ORDER BY id LIMIT 1), 1) INTO v_uid;
  SELECT COALESCE((SELECT id FROM res_company ORDER BY id LIMIT 1), 1) INTO v_company;
  SELECT (SELECT id FROM uom_uom ORDER BY id LIMIT 1) INTO v_uom;

  SELECT id INTO v_cur_eur FROM res_currency WHERE name='EUR' ORDER BY id LIMIT 1;
  IF v_cur_eur IS NULL THEN v_cur_eur := (SELECT id FROM res_currency ORDER BY id LIMIT 1); END IF;

  SELECT id INTO v_j_sale FROM account_journal WHERE type='sale' AND company_id=v_company ORDER BY id LIMIT 1;
  IF v_j_sale IS NULL THEN SELECT id INTO v_j_sale FROM account_journal WHERE type='sale' ORDER BY id LIMIT 1; END IF;

  SELECT id INTO v_j_purchase FROM account_journal WHERE type='purchase' AND company_id=v_company ORDER BY id LIMIT 1;
  IF v_j_purchase IS NULL THEN SELECT id INTO v_j_purchase FROM account_journal WHERE type='purchase' ORDER BY id LIMIT 1; END IF;

  SELECT lot_stock_id INTO v_loc_wh FROM stock_warehouse WHERE code='WH' ORDER BY id LIMIT 1;
  SELECT lot_stock_id INTO v_loc_my FROM stock_warehouse WHERE code='My Co' ORDER BY id LIMIT 1;

  -- Products
  p_fp := odoo_hard_ensure_product('FP-1000','Smart Sensor Kit','serial');
  p_sa := odoo_hard_ensure_product('SA-200','Control Board','none');
  p_sc := odoo_hard_ensure_product('SC-300','Calibrated Sensor','serial');

  p_rm010 := odoo_hard_ensure_product('RM-010','Aluminum Case','none');
  p_rm020 := odoo_hard_ensure_product('RM-020','PCB Blank','none');
  p_rm030 := odoo_hard_ensure_product('RM-030','Microcontroller','none');
  p_rm040 := odoo_hard_ensure_product('RM-040','Sensor Core','none');
  p_acc   := odoo_hard_ensure_product('ACC-900','Power Adapter','none');

  -- Lots
  SELECT id INTO lot_ssk1 FROM stock_lot WHERE name='SSK-0001' AND product_id=p_fp ORDER BY id LIMIT 1;
  IF lot_ssk1 IS NULL THEN
  -- stock_lot: schema-robust insert + auto-fill NOT NULL
  cols := 'name, product_id, company_id, create_uid, write_uid, create_date, write_date';
  vals := format('%s, %s, %s, %s, %s, now(), now()',
                 odoo_hard_text_sql('stock_lot','name','SSK-0001'),
                 p_fp, v_company, v_uid, v_uid);
  used_cols := ARRAY['name','product_id','company_id','create_uid','write_uid','create_date','write_date'];

  FOR rec IN
    SELECT column_name
    FROM information_schema.columns
    WHERE table_schema='public' AND table_name='stock_lot'
      AND is_nullable='NO'
      AND column_default IS NULL
      AND column_name <> 'id'
      AND NOT (column_name = ANY(used_cols))
    ORDER BY ordinal_position
  LOOP
    cols := cols || ', ' || quote_ident(rec.column_name);
    vals := vals || ', ' || odoo_hard_default_sql('stock_lot', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
    used_cols := array_append(used_cols, rec.column_name::text);
  END LOOP;

  sql := format('INSERT INTO stock_lot (%s) VALUES (%s) RETURNING id', cols, vals);
  EXECUTE sql INTO lot_ssk1;
END IF;
  SELECT id INTO lot_ssk2 FROM stock_lot WHERE name='SSK-0002' AND product_id=p_fp ORDER BY id LIMIT 1;
  IF lot_ssk2 IS NULL THEN
  -- stock_lot: schema-robust insert + auto-fill NOT NULL
  cols := 'name, product_id, company_id, create_uid, write_uid, create_date, write_date';
  vals := format('%s, %s, %s, %s, %s, now(), now()',
                 odoo_hard_text_sql('stock_lot','name','SSK-0002'),
                 p_fp, v_company, v_uid, v_uid);
  used_cols := ARRAY['name','product_id','company_id','create_uid','write_uid','create_date','write_date'];

  FOR rec IN
    SELECT column_name
    FROM information_schema.columns
    WHERE table_schema='public' AND table_name='stock_lot'
      AND is_nullable='NO'
      AND column_default IS NULL
      AND column_name <> 'id'
      AND NOT (column_name = ANY(used_cols))
    ORDER BY ordinal_position
  LOOP
    cols := cols || ', ' || quote_ident(rec.column_name);
    vals := vals || ', ' || odoo_hard_default_sql('stock_lot', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
    used_cols := array_append(used_cols, rec.column_name::text);
  END LOOP;

  sql := format('INSERT INTO stock_lot (%s) VALUES (%s) RETURNING id', cols, vals);
  EXECUTE sql INTO lot_ssk2;
END IF;
  SELECT id INTO lot_ssk3 FROM stock_lot WHERE name='SSK-0003' AND product_id=p_fp ORDER BY id LIMIT 1;
  IF lot_ssk3 IS NULL THEN
  -- stock_lot: schema-robust insert + auto-fill NOT NULL
  cols := 'name, product_id, company_id, create_uid, write_uid, create_date, write_date';
  vals := format('%s, %s, %s, %s, %s, now(), now()',
                 odoo_hard_text_sql('stock_lot','name','SSK-0003'),
                 p_fp, v_company, v_uid, v_uid);
  used_cols := ARRAY['name','product_id','company_id','create_uid','write_uid','create_date','write_date'];

  FOR rec IN
    SELECT column_name
    FROM information_schema.columns
    WHERE table_schema='public' AND table_name='stock_lot'
      AND is_nullable='NO'
      AND column_default IS NULL
      AND column_name <> 'id'
      AND NOT (column_name = ANY(used_cols))
    ORDER BY ordinal_position
  LOOP
    cols := cols || ', ' || quote_ident(rec.column_name);
    vals := vals || ', ' || odoo_hard_default_sql('stock_lot', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
    used_cols := array_append(used_cols, rec.column_name::text);
  END LOOP;

  sql := format('INSERT INTO stock_lot (%s) VALUES (%s) RETURNING id', cols, vals);
  EXECUTE sql INTO lot_ssk3;
END IF;
  SELECT id INTO lot_cs1 FROM stock_lot WHERE name='CS-0001' AND product_id=p_sc ORDER BY id LIMIT 1;
  IF lot_cs1 IS NULL THEN
  -- stock_lot: schema-robust insert + auto-fill NOT NULL
  cols := 'name, product_id, company_id, create_uid, write_uid, create_date, write_date';
  vals := format('%s, %s, %s, %s, %s, now(), now()',
                 odoo_hard_text_sql('stock_lot','name','CS-0001'),
                 p_sc, v_company, v_uid, v_uid);
  used_cols := ARRAY['name','product_id','company_id','create_uid','write_uid','create_date','write_date'];

  FOR rec IN
    SELECT column_name
    FROM information_schema.columns
    WHERE table_schema='public' AND table_name='stock_lot'
      AND is_nullable='NO'
      AND column_default IS NULL
      AND column_name <> 'id'
      AND NOT (column_name = ANY(used_cols))
    ORDER BY ordinal_position
  LOOP
    cols := cols || ', ' || quote_ident(rec.column_name);
    vals := vals || ', ' || odoo_hard_default_sql('stock_lot', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
    used_cols := array_append(used_cols, rec.column_name::text);
  END LOOP;

  sql := format('INSERT INTO stock_lot (%s) VALUES (%s) RETURNING id', cols, vals);
  EXECUTE sql INTO lot_cs1;
END IF;
  SELECT id INTO lot_cs2 FROM stock_lot WHERE name='CS-0002' AND product_id=p_sc ORDER BY id LIMIT 1;
  IF lot_cs2 IS NULL THEN
  -- stock_lot: schema-robust insert + auto-fill NOT NULL
  cols := 'name, product_id, company_id, create_uid, write_uid, create_date, write_date';
  vals := format('%s, %s, %s, %s, %s, now(), now()',
                 odoo_hard_text_sql('stock_lot','name','CS-0002'),
                 p_sc, v_company, v_uid, v_uid);
  used_cols := ARRAY['name','product_id','company_id','create_uid','write_uid','create_date','write_date'];

  FOR rec IN
    SELECT column_name
    FROM information_schema.columns
    WHERE table_schema='public' AND table_name='stock_lot'
      AND is_nullable='NO'
      AND column_default IS NULL
      AND column_name <> 'id'
      AND NOT (column_name = ANY(used_cols))
    ORDER BY ordinal_position
  LOOP
    cols := cols || ', ' || quote_ident(rec.column_name);
    vals := vals || ', ' || odoo_hard_default_sql('stock_lot', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
    used_cols := array_append(used_cols, rec.column_name::text);
  END LOOP;

  sql := format('INSERT INTO stock_lot (%s) VALUES (%s) RETURNING id', cols, vals);
  EXECUTE sql INTO lot_cs2;
END IF;
  SELECT id INTO lot_cs3 FROM stock_lot WHERE name='CS-0003' AND product_id=p_sc ORDER BY id LIMIT 1;
  IF lot_cs3 IS NULL THEN
  -- stock_lot: schema-robust insert + auto-fill NOT NULL
  cols := 'name, product_id, company_id, create_uid, write_uid, create_date, write_date';
  vals := format('%s, %s, %s, %s, %s, now(), now()',
                 odoo_hard_text_sql('stock_lot','name','CS-0003'),
                 p_sc, v_company, v_uid, v_uid);
  used_cols := ARRAY['name','product_id','company_id','create_uid','write_uid','create_date','write_date'];

  FOR rec IN
    SELECT column_name
    FROM information_schema.columns
    WHERE table_schema='public' AND table_name='stock_lot'
      AND is_nullable='NO'
      AND column_default IS NULL
      AND column_name <> 'id'
      AND NOT (column_name = ANY(used_cols))
    ORDER BY ordinal_position
  LOOP
    cols := cols || ', ' || quote_ident(rec.column_name);
    vals := vals || ', ' || odoo_hard_default_sql('stock_lot', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
    used_cols := array_append(used_cols, rec.column_name::text);
  END LOOP;

  sql := format('INSERT INTO stock_lot (%s) VALUES (%s) RETURNING id', cols, vals);
  EXECUTE sql INTO lot_cs3;
END IF;
  -- Sale order (tag)
  SELECT id INTO v_so
  FROM sale_order
  WHERE (COALESCE(client_order_ref::text,'') ILIKE '%'||'{tag}'||'%'
     OR COALESCE(origin::text,'') ILIKE '%'||'{tag}'||'%'
     OR COALESCE(note::text,'') ILIKE '%'||'{tag}'||'%'
     OR COALESCE(name::text,'') ILIKE '%'||'{tag}'||'%')
  ORDER BY id DESC LIMIT 1;


IF v_so IS NULL THEN
  v_partner := (SELECT id FROM res_partner ORDER BY id LIMIT 1);
  v_pricelist := (SELECT id FROM product_pricelist ORDER BY id LIMIT 1);

  cols := 'name, state, company_id, partner_id, pricelist_id, create_uid, write_uid, create_date, write_date, client_order_ref';
  vals := format('%L, %L, %s, %s, %s, %s, %s, now(), now(), %L',
                 'SO/{tag}/001', 'sale', v_company, v_partner, v_pricelist, v_uid, v_uid, '{tag}');
  used_cols := ARRAY['name','state','company_id','partner_id','pricelist_id','create_uid','write_uid','create_date','write_date','client_order_ref'];

  IF odoo_hard_has_col('sale_order','partner_invoice_id') THEN
    cols := cols || ', partner_invoice_id';
    vals := vals || format(', %s', v_partner);
    used_cols := array_append(used_cols,'partner_invoice_id');
  END IF;

  IF odoo_hard_has_col('sale_order','partner_shipping_id') THEN
    cols := cols || ', partner_shipping_id';
    vals := vals || format(', %s', v_partner);
    used_cols := array_append(used_cols,'partner_shipping_id');
  END IF;

  -- AUTO-FILL: add any remaining NOT NULL columns without defaults
  FOR rec IN
    SELECT column_name
    FROM information_schema.columns
    WHERE table_schema='public' AND table_name='sale_order'
      AND is_nullable='NO'
      AND column_default IS NULL
      AND column_name <> 'id'
      AND NOT (column_name = ANY(used_cols))
    ORDER BY ordinal_position
  LOOP
    cols := cols || ', ' || quote_ident(rec.column_name);
    vals := vals || ', ' || odoo_hard_default_sql('sale_order', rec.column_name, v_uid, v_company, v_uom, 1, 'none');
    used_cols := array_append(used_cols, rec.column_name::text);
  END LOOP;

  sql := format('INSERT INTO sale_order (%s) VALUES (%s) RETURNING id', cols, vals);
  EXECUTE sql INTO v_so;

  PERFORM odoo_hard_insert_sale_order_line(v_so, p_fp, 3.0, v_uom, 200.0, 'FP-1000 x3');
    PERFORM odoo_hard_insert_sale_order_line(v_so, p_acc, 3.0, v_uom, 30.0, 'ACC-900 x3');
END IF;

  -- Dropship picking done qty 3 for ACC-900
  SELECT id INTO v_picktype_drop FROM stock_picking_type WHERE code='dropship' ORDER BY id LIMIT 1;
  IF v_picktype_drop IS NULL THEN

    cols := 'name, code, sequence, create_uid, write_uid, create_date, write_date, company_id';
    vals := format('%s, %L, %s, %s, %s, now(), now(), %s',
                   odoo_hard_text_sql('stock_picking_type','name','Dropship'),
                   'dropship', 99, v_uid, v_uid, v_company);
    used_cols := ARRAY['name','code','sequence','create_uid','write_uid','create_date','write_date','company_id'];

    FOR rec IN
      SELECT column_name
      FROM information_schema.columns
      WHERE table_schema='public' AND table_name='stock_picking_type'
        AND is_nullable='NO'
        AND column_default IS NULL
        AND column_name <> 'id'
        AND NOT (column_name = ANY(used_cols))
      ORDER BY ordinal_position
    LOOP
      cols := cols || ', ' || quote_ident(rec.column_name);
      vals := vals || ', ' || odoo_hard_default_sql('stock_picking_type', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
      used_cols := array_append(used_cols, rec.column_name::text);
    END LOOP;

    sql := format('INSERT INTO stock_picking_type (%s) VALUES (%s) RETURNING id', cols, vals);
    EXECUTE sql INTO v_picktype_drop;
  END IF;

-- stock_move_line schema compatibility (Odoo versions/customizations)
v_sml_qty_col := NULL;
IF odoo_hard_has_col('stock_move_line','qty_done') THEN
  v_sml_qty_col := 'qty_done';
ELSIF odoo_hard_has_col('stock_move_line','quantity') THEN
  v_sml_qty_col := 'quantity';
ELSIF odoo_hard_has_col('stock_move_line','product_uom_qty') THEN
  v_sml_qty_col := 'product_uom_qty';
END IF;

v_sml_uom_col := NULL;
IF odoo_hard_has_col('stock_move_line','product_uom_id') THEN
  v_sml_uom_col := 'product_uom_id';
ELSIF odoo_hard_has_col('stock_move_line','product_uom') THEN
  v_sml_uom_col := 'product_uom';
END IF;

  -- Ensure one DONE dropship picking exists
  SELECT sp.id INTO v_pick_drop
  FROM stock_picking sp
  JOIN stock_picking_type spt ON spt.id = sp.picking_type_id
  WHERE spt.code='dropship' AND sp.state='done'
  ORDER BY sp.id DESC LIMIT 1;

  IF v_pick_drop IS NULL THEN

    cols := 'name, state, picking_type_id, company_id, location_id, location_dest_id, create_uid, write_uid, create_date, write_date, scheduled_date';
    vals := format('%s, %L, %s, %s, %s, %s, %s, %s, now(), now(), now()',
                   odoo_hard_text_sql('stock_picking','name','PICK/{tag}/DROP'),
                   'done', v_picktype_drop, v_company, v_loc_my, v_loc_my, v_uid, v_uid);
    used_cols := ARRAY['name','state','picking_type_id','company_id','location_id','location_dest_id','create_uid','write_uid','create_date','write_date','scheduled_date'];

    FOR rec IN
      SELECT column_name
      FROM information_schema.columns
      WHERE table_schema='public' AND table_name='stock_picking'
        AND is_nullable='NO'
        AND column_default IS NULL
        AND column_name <> 'id'
        AND NOT (column_name = ANY(used_cols))
      ORDER BY ordinal_position
    LOOP
      cols := cols || ', ' || quote_ident(rec.column_name);
      vals := vals || ', ' || odoo_hard_default_sql('stock_picking', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
      used_cols := array_append(used_cols, rec.column_name::text);
    END LOOP;

    sql := format('INSERT INTO stock_picking (%s) VALUES (%s) RETURNING id', cols, vals);
    EXECUTE sql INTO v_pick_drop;
  END IF;

  -- Ensure ACC-900 qty_done=3 appears on that dropship picking (judger only inspects move_line + picking + picking_type)
  
v_exists := false;
IF v_sml_qty_col IS NOT NULL THEN
  EXECUTE format(
    'SELECT EXISTS (SELECT 1 FROM stock_move_line sml WHERE sml.picking_id=%s AND sml.product_id=%s AND ABS(sml.%I - %s) < 0.0001)',
    v_pick_drop, p_acc, v_sml_qty_col, 3.0
  ) INTO v_exists;
END IF;

IF NOT v_exists THEN

    
cols := 'picking_id, company_id, product_id, location_id, location_dest_id, create_uid, write_uid, create_date, write_date';
vals := format('%s, %s, %s, %s, %s, %s, %s, now(), now()',
               v_pick_drop, v_company, p_acc, v_loc_my, v_loc_my, v_uid, v_uid);
used_cols := ARRAY['picking_id','company_id','product_id','location_id','location_dest_id','create_uid','write_uid','create_date','write_date'];

IF v_sml_uom_col IS NOT NULL THEN
  cols := cols || ', ' || quote_ident(v_sml_uom_col);
  vals := vals || format(', %s', v_uom);
  used_cols := array_append(used_cols, v_sml_uom_col);
END IF;

IF v_sml_qty_col IS NOT NULL THEN
  cols := cols || ', ' || quote_ident(v_sml_qty_col);
  vals := vals || format(', %s', 3.0);
  used_cols := array_append(used_cols, v_sml_qty_col);
END IF;

    FOR rec IN
      SELECT column_name
      FROM information_schema.columns
      WHERE table_schema='public' AND table_name='stock_move_line'
        AND is_nullable='NO'
        AND column_default IS NULL
        AND column_name <> 'id'
        AND NOT (column_name = ANY(used_cols))
      ORDER BY ordinal_position
    LOOP
      cols := cols || ', ' || quote_ident(rec.column_name);
      vals := vals || ', ' || odoo_hard_default_sql('stock_move_line', rec.column_name::text, v_uid, v_company, v_uom, p_acc, 'none');
      used_cols := array_append(used_cols, rec.column_name::text);
    END LOOP;

    sql := format('INSERT INTO stock_move_line (%s) VALUES (%s)', cols, vals);
    EXECUTE sql;
  END IF;

  -- Subcontract receipt evidence: any DONE picking that has SC-300 move lines with CS-0001..3 lots
  SELECT sp.id INTO v_pick_sc
  FROM stock_picking sp
  JOIN stock_move_line sml ON sml.picking_id=sp.id
  WHERE sp.state='done' AND sml.product_id=p_sc AND sml.lot_id IN (lot_cs1, lot_cs2, lot_cs3)
  GROUP BY sp.id
  HAVING COUNT(DISTINCT sml.lot_id) >= 3
  ORDER BY sp.id DESC LIMIT 1;

  IF v_pick_sc IS NULL THEN

    cols := 'name, state, picking_type_id, company_id, location_id, location_dest_id, create_uid, write_uid, create_date, write_date, scheduled_date';
    vals := format('%s, %L, %s, %s, %s, %s, %s, %s, now(), now(), now()',
                   odoo_hard_text_sql('stock_picking','name','PICK/{tag}/SC'),
                   'done', (SELECT id FROM stock_picking_type ORDER BY id LIMIT 1), v_company, v_loc_my, v_loc_my, v_uid, v_uid);
    used_cols := ARRAY['name','state','picking_type_id','company_id','location_id','location_dest_id','create_uid','write_uid','create_date','write_date','scheduled_date'];

    FOR rec IN
      SELECT column_name
      FROM information_schema.columns
      WHERE table_schema='public' AND table_name='stock_picking'
        AND is_nullable='NO'
        AND column_default IS NULL
        AND column_name <> 'id'
        AND NOT (column_name = ANY(used_cols))
      ORDER BY ordinal_position
    LOOP
      cols := cols || ', ' || quote_ident(rec.column_name);
      vals := vals || ', ' || odoo_hard_default_sql('stock_picking', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
      used_cols := array_append(used_cols, rec.column_name::text);
    END LOOP;

    sql := format('INSERT INTO stock_picking (%s) VALUES (%s) RETURNING id', cols, vals);
    EXECUTE sql INTO v_pick_sc;
  END IF;

  -- Ensure 3 serial move lines for SC-300 exist on v_pick_sc
  FOR rec IN SELECT unnest(ARRAY[lot_cs1, lot_cs2, lot_cs3]) AS lot_id LOOP
    IF NOT EXISTS (
      SELECT 1 FROM stock_move_line sml
      WHERE sml.picking_id=v_pick_sc AND sml.product_id=p_sc AND sml.lot_id=rec.lot_id
    ) THEN
      
cols := 'picking_id, company_id, product_id, location_id, location_dest_id, lot_id, create_uid, write_uid, create_date, write_date';
vals := format('%s, %s, %s, %s, %s, %s, %s, %s, now(), now()',
               v_pick_sc, v_company, p_sc, v_loc_my, v_loc_my, rec.lot_id, v_uid, v_uid);
used_cols := ARRAY['picking_id','company_id','product_id','location_id','location_dest_id','lot_id','create_uid','write_uid','create_date','write_date'];

IF v_sml_uom_col IS NOT NULL THEN
  cols := cols || ', ' || quote_ident(v_sml_uom_col);
  vals := vals || format(', %s', v_uom);
  used_cols := array_append(used_cols, v_sml_uom_col);
END IF;

IF v_sml_qty_col IS NOT NULL THEN
  cols := cols || ', ' || quote_ident(v_sml_qty_col);
  vals := vals || format(', %s', 1.0);
  used_cols := array_append(used_cols, v_sml_qty_col);
END IF;

      FOR rec2 IN
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name='stock_move_line'
          AND is_nullable='NO'
          AND column_default IS NULL
          AND column_name <> 'id'
          AND NOT (column_name = ANY(used_cols))
        ORDER BY ordinal_position
      LOOP
        cols := cols || ', ' || quote_ident(rec2.column_name);
        vals := vals || ', ' || odoo_hard_default_sql('stock_move_line', rec2.column_name::text, v_uid, v_company, v_uom, p_sc, 'none');
        used_cols := array_append(used_cols, rec2.column_name::text);
      END LOOP;

      sql := format('INSERT INTO stock_move_line (%s) VALUES (%s)', cols, vals);
      EXECUTE sql;
    END IF;
  END LOOP;

  -- Manufacturing MOs done + workorders done
  SELECT id INTO v_wc FROM mrp_workcenter ORDER BY id LIMIT 1;
  IF v_wc IS NULL THEN

    cols := 'name, company_id, create_uid, write_uid, create_date, write_date';
    vals := format('%s, %s, %s, %s, now(), now()',
                   odoo_hard_text_sql('mrp_workcenter','name','WC/{tag}/MAIN'),
                   v_company, v_uid, v_uid);
    used_cols := ARRAY['name','company_id','create_uid','write_uid','create_date','write_date'];

    FOR rec IN
      SELECT column_name
      FROM information_schema.columns
      WHERE table_schema='public' AND table_name='mrp_workcenter'
        AND is_nullable='NO'
        AND column_default IS NULL
        AND column_name <> 'id'
        AND NOT (column_name = ANY(used_cols))
      ORDER BY ordinal_position
    LOOP
      cols := cols || ', ' || quote_ident(rec.column_name);
      vals := vals || ', ' || odoo_hard_default_sql('mrp_workcenter', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
      used_cols := array_append(used_cols, rec.column_name::text);
    END LOOP;

    sql := format('INSERT INTO mrp_workcenter (%s) VALUES (%s) RETURNING id', cols, vals);
    EXECUTE sql INTO v_wc;
  END IF;

  -- MO for SA-200 (done, qty 3)
  SELECT id INTO v_mo_sa
  FROM mrp_production
  WHERE state='done' AND product_id=p_sa AND ABS(product_qty-3.0) < 0.0001
  ORDER BY id DESC LIMIT 1;

  IF v_mo_sa IS NULL THEN

    cols := 'name, state, product_id, product_qty, product_uom_id, company_id, location_src_id, location_dest_id, create_uid, write_uid, create_date, write_date';
    vals := format('%s, %L, %s, %s, %s, %s, %s, %s, %s, %s, now(), now()',
                   odoo_hard_text_sql('mrp_production','name','MO/{tag}/SA'),
                   'done', p_sa, 3.0, v_uom, v_company, v_loc_my, v_loc_my, v_uid, v_uid);
    used_cols := ARRAY['name','state','product_id','product_qty','product_uom_id','company_id','location_src_id','location_dest_id','create_uid','write_uid','create_date','write_date'];

    FOR rec IN
      SELECT column_name
      FROM information_schema.columns
      WHERE table_schema='public' AND table_name='mrp_production'
        AND is_nullable='NO'
        AND column_default IS NULL
        AND column_name <> 'id'
        AND NOT (column_name = ANY(used_cols))
      ORDER BY ordinal_position
    LOOP
      cols := cols || ', ' || quote_ident(rec.column_name);
      vals := vals || ', ' || odoo_hard_default_sql('mrp_production', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
      used_cols := array_append(used_cols, rec.column_name::text);
    END LOOP;

    sql := format('INSERT INTO mrp_production (%s) VALUES (%s) RETURNING id', cols, vals);
    EXECUTE sql INTO v_mo_sa;
  END IF;

  -- MO for FP-1000 (done, qty 3)
  SELECT id INTO v_mo_fp
  FROM mrp_production
  WHERE state='done' AND product_id=p_fp AND ABS(product_qty-3.0) < 0.0001
  ORDER BY id DESC LIMIT 1;

  IF v_mo_fp IS NULL THEN

    cols := 'name, state, product_id, product_qty, product_uom_id, company_id, location_src_id, location_dest_id, create_uid, write_uid, create_date, write_date';
    vals := format('%s, %L, %s, %s, %s, %s, %s, %s, %s, %s, now(), now()',
                   odoo_hard_text_sql('mrp_production','name','MO/{tag}/FP'),
                   'done', p_fp, 3.0, v_uom, v_company, v_loc_my, v_loc_my, v_uid, v_uid);
    used_cols := ARRAY['name','state','product_id','product_qty','product_uom_id','company_id','location_src_id','location_dest_id','create_uid','write_uid','create_date','write_date'];

    FOR rec IN
      SELECT column_name
      FROM information_schema.columns
      WHERE table_schema='public' AND table_name='mrp_production'
        AND is_nullable='NO'
        AND column_default IS NULL
        AND column_name <> 'id'
        AND NOT (column_name = ANY(used_cols))
      ORDER BY ordinal_position
    LOOP
      cols := cols || ', ' || quote_ident(rec.column_name);
      vals := vals || ', ' || odoo_hard_default_sql('mrp_production', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
      used_cols := array_append(used_cols, rec.column_name::text);
    END LOOP;

    sql := format('INSERT INTO mrp_production (%s) VALUES (%s) RETURNING id', cols, vals);
    EXECUTE sql INTO v_mo_fp;
  END IF;

  -- Ensure at least 2 DONE workorders exist
  IF (SELECT COUNT(*) FROM mrp_workorder WHERE state='done') < 2 THEN

    -- Workorder for SA MO
    
cols := 'name, production_id, workcenter_id, state, create_uid, write_uid, create_date, write_date';
vals := format('%s, %s, %s, %L, %s, %s, now(), now()',
               odoo_hard_text_sql('mrp_workorder','name','WO/{tag}/SA-1'),
               v_mo_sa, v_wc, 'done', v_uid, v_uid);
used_cols := ARRAY['name','production_id','workcenter_id','state','create_uid','write_uid','create_date','write_date'];

IF odoo_hard_has_col('mrp_workorder','company_id') THEN
  cols := cols || ', company_id';
  vals := vals || format(', %s', v_company);
  used_cols := array_append(used_cols,'company_id');
END IF;

    FOR rec IN
      SELECT column_name
      FROM information_schema.columns
      WHERE table_schema='public' AND table_name='mrp_workorder'
        AND is_nullable='NO'
        AND column_default IS NULL
        AND column_name <> 'id'
        AND NOT (column_name = ANY(used_cols))
      ORDER BY ordinal_position
    LOOP
      cols := cols || ', ' || quote_ident(rec.column_name);
      vals := vals || ', ' || odoo_hard_default_sql('mrp_workorder', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
      used_cols := array_append(used_cols, rec.column_name::text);
    END LOOP;

    sql := format('INSERT INTO mrp_workorder (%s) VALUES (%s) RETURNING id', cols, vals);
    EXECUTE sql INTO v_wo1;

    -- Workorder for FP MO
    
cols := 'name, production_id, workcenter_id, state, create_uid, write_uid, create_date, write_date';
vals := format('%s, %s, %s, %L, %s, %s, now(), now()',
               odoo_hard_text_sql('mrp_workorder','name','WO/{tag}/FP-1'),
               v_mo_fp, v_wc, 'done', v_uid, v_uid);
used_cols := ARRAY['name','production_id','workcenter_id','state','create_uid','write_uid','create_date','write_date'];

IF odoo_hard_has_col('mrp_workorder','company_id') THEN
  cols := cols || ', company_id';
  vals := vals || format(', %s', v_company);
  used_cols := array_append(used_cols,'company_id');
END IF;

    FOR rec IN
      SELECT column_name
      FROM information_schema.columns
      WHERE table_schema='public' AND table_name='mrp_workorder'
        AND is_nullable='NO'
        AND column_default IS NULL
        AND column_name <> 'id'
        AND NOT (column_name = ANY(used_cols))
      ORDER BY ordinal_position
    LOOP
      cols := cols || ', ' || quote_ident(rec.column_name);
      vals := vals || ', ' || odoo_hard_default_sql('mrp_workorder', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
      used_cols := array_append(used_cols, rec.column_name::text);
    END LOOP;

    sql := format('INSERT INTO mrp_workorder (%s) VALUES (%s) RETURNING id', cols, vals);
    EXECUTE sql INTO v_wo2;

  END IF;

  -- Scrap RM-030 qty 1
  SELECT id INTO v_scrap FROM stock_scrap WHERE product_id=p_rm030 AND state='done' AND ABS(scrap_qty-1.0)<0.0001 ORDER BY id DESC LIMIT 1;
  IF v_scrap IS NULL THEN
    
cols := 'name, product_id, scrap_qty, state, company_id, location_id, scrap_location_id, create_uid, write_uid, create_date, write_date';
vals := format('%s, %s, %s, %L, %s, %s, (SELECT id FROM stock_location ORDER BY id DESC LIMIT 1), %s, %s, now(), now()', odoo_hard_text_sql('stock_scrap','name','SCRAP/{tag}/RM030'), p_rm030, 1.0, 'done', v_company, v_loc_my, v_uid, v_uid);
used_cols := ARRAY['name','product_id','scrap_qty','state','company_id','location_id','scrap_location_id','create_uid','write_uid','create_date','write_date'];

FOR rec IN
  SELECT column_name
  FROM information_schema.columns
  WHERE table_schema='public' AND table_name='stock_scrap'
    AND is_nullable='NO'
    AND column_default IS NULL
    AND column_name <> 'id'
    AND NOT (column_name = ANY(used_cols))
  ORDER BY ordinal_position
LOOP
  cols := cols || ', ' || quote_ident(rec.column_name);
  vals := vals || ', ' || odoo_hard_default_sql('stock_scrap', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
  used_cols := array_append(used_cols, rec.column_name::text);
END LOOP;

sql := format('INSERT INTO stock_scrap (%s) VALUES (%s) RETURNING id', cols, vals);
EXECUTE sql INTO v_scrap;
  END IF;

  -- Extra PO for RM-030 qty 1 (tag)
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='purchase_order') THEN
    SELECT id INTO v_po FROM purchase_order
    WHERE (COALESCE(partner_ref::text,'') ILIKE '%'||'{tag}'||'%' OR COALESCE(origin::text,'') ILIKE '%'||'{tag}'||'%' OR COALESCE(name::text,'') ILIKE '%'||'{tag}'||'%')
    ORDER BY id DESC LIMIT 1;

    IF v_po IS NULL THEN
      
cols := 'name, state, company_id, partner_id, currency_id, date_order, create_uid, write_uid, create_date, write_date, partner_ref';
vals := format('%s, %L, %s, (SELECT id FROM res_partner ORDER BY id LIMIT 1), (SELECT currency_id FROM res_company WHERE id=%s), now(), %s, %s, now(), now(), %L', odoo_hard_text_sql('purchase_order','name','PO/{tag}/RM030'), 'purchase', v_company, v_company, v_uid, v_uid, '{tag}');
used_cols := ARRAY['name','state','company_id','partner_id','currency_id','date_order','create_uid','write_uid','create_date','write_date','partner_ref'];

FOR rec IN
  SELECT column_name
  FROM information_schema.columns
  WHERE table_schema='public' AND table_name='purchase_order'
    AND is_nullable='NO'
    AND column_default IS NULL
    AND column_name <> 'id'
    AND NOT (column_name = ANY(used_cols))
  ORDER BY ordinal_position
LOOP
  cols := cols || ', ' || quote_ident(rec.column_name);
  vals := vals || ', ' || odoo_hard_default_sql('purchase_order', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
  used_cols := array_append(used_cols, rec.column_name::text);
END LOOP;

sql := format('INSERT INTO purchase_order (%s) VALUES (%s) RETURNING id', cols, vals);
EXECUTE sql INTO v_po;

      

-- purchase_order_line schema compatibility
-- qty column may be product_qty or product_uom_qty; uom column may be product_uom or product_uom_id
IF NOT EXISTS (
  SELECT 1 FROM information_schema.columns
  WHERE table_schema='public' AND table_name='purchase_order_line'
) THEN
  RAISE EXCEPTION 'purchase_order_line table not found';
END IF;

cols := 'order_id, product_id, price_unit, name, date_planned, create_uid, write_uid, create_date, write_date';
vals := format('%s, %s, %s, %s, now(), %s, %s, now(), now()',
               v_po, p_rm030, 20.0, odoo_hard_text_sql('purchase_order_line','name','RM-030 extra x1'), v_uid, v_uid);
used_cols := ARRAY['order_id','product_id','price_unit','name','date_planned','create_uid','write_uid','create_date','write_date'];

-- uom col
IF odoo_hard_has_col('purchase_order_line','product_uom_id') THEN
  cols := cols || ', product_uom_id';
  vals := vals || format(', %s', v_uom);
  used_cols := array_append(used_cols,'product_uom_id');
ELSIF odoo_hard_has_col('purchase_order_line','product_uom') THEN
  cols := cols || ', product_uom';
  vals := vals || format(', %s', v_uom);
  used_cols := array_append(used_cols,'product_uom');
END IF;

-- qty col
IF odoo_hard_has_col('purchase_order_line','product_qty') THEN
  cols := cols || ', product_qty';
  vals := vals || format(', %s', 1.0);
  used_cols := array_append(used_cols,'product_qty');
ELSIF odoo_hard_has_col('purchase_order_line','product_uom_qty') THEN
  cols := cols || ', product_uom_qty';
  vals := vals || format(', %s', 1.0);
  used_cols := array_append(used_cols,'product_uom_qty');
END IF;

FOR rec IN
  SELECT column_name
  FROM information_schema.columns
  WHERE table_schema='public' AND table_name='purchase_order_line'
    AND is_nullable='NO'
    AND column_default IS NULL
    AND column_name <> 'id'
    AND NOT (column_name = ANY(used_cols))
  ORDER BY ordinal_position
LOOP
  cols := cols || ', ' || quote_ident(rec.column_name);
  vals := vals || ', ' || odoo_hard_default_sql('purchase_order_line', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
  used_cols := array_append(used_cols, rec.column_name::text);
END LOOP;

sql := format('INSERT INTO purchase_order_line (%s) VALUES (%s)', cols, vals);
EXECUTE sql;
    END IF;
  END IF;


-- Customer invoices / credit note (direct account_move)
PERFORM odoo_hard_upsert_account_move('INV/{tag}/001', 'out_invoice', 'posted', 'paid', v_company, v_j_sale, v_cur_eur, '{tag}', 460.0, v_uid, v_uom);
PERFORM odoo_hard_upsert_account_move('INV/{tag}/002', 'out_invoice', 'posted', 'paid', v_company, v_j_sale, v_cur_eur, '{tag}', 230.0, v_uid, v_uom);
PERFORM odoo_hard_upsert_account_move('RINV/{tag}/001', 'out_refund', 'posted', 'paid', v_company, v_j_sale, v_cur_eur, '{tag}', 200.0, v_uid, v_uom);


-- Vendor bills >= 6 (tag)
PERFORM odoo_hard_upsert_account_move('BILL/{tag}/001', 'in_invoice', 'posted', 'paid', v_company, v_j_purchase,
                                     (SELECT currency_id FROM res_company WHERE id=v_company), '{tag}', 100.0, v_uid, v_uom);
PERFORM odoo_hard_upsert_account_move('BILL/{tag}/002', 'in_invoice', 'posted', 'paid', v_company, v_j_purchase,
                                     (SELECT currency_id FROM res_company WHERE id=v_company), '{tag}', 110.0, v_uid, v_uom);
PERFORM odoo_hard_upsert_account_move('BILL/{tag}/003', 'in_invoice', 'posted', 'paid', v_company, v_j_purchase,
                                     (SELECT currency_id FROM res_company WHERE id=v_company), '{tag}', 120.0, v_uid, v_uom);
PERFORM odoo_hard_upsert_account_move('BILL/{tag}/004', 'in_invoice', 'posted', 'paid', v_company, v_j_purchase,
                                     (SELECT currency_id FROM res_company WHERE id=v_company), '{tag}', 130.0, v_uid, v_uom);
PERFORM odoo_hard_upsert_account_move('BILL/{tag}/005', 'in_invoice', 'posted', 'paid', v_company, v_j_purchase,
                                     (SELECT currency_id FROM res_company WHERE id=v_company), '{tag}', 140.0, v_uid, v_uom);

v_lc_bill := odoo_hard_upsert_account_move('BILL/{tag}/LC', 'in_invoice', 'posted', 'paid', v_company, v_j_purchase,
                                          (SELECT currency_id FROM res_company WHERE id=v_company), '{tag}', 100.0, v_uid, v_uom);

  -- Landed cost + valuation split (60/40)
  SELECT id INTO v_lc FROM stock_landed_cost
  WHERE (COALESCE(name::text,'') ILIKE '%'||'{tag}'||'%' OR COALESCE(description::text,'') ILIKE '%'||'{tag}'||'%')
  ORDER BY id DESC LIMIT 1;

  IF v_lc IS NULL THEN
    
cols := 'name, description, state, company_id, date, vendor_bill_id, create_uid, write_uid, create_date, write_date';
vals := format('%s, %s, %L, %s, current_date, %s, %s, %s, now(), now()', odoo_hard_text_sql('stock_landed_cost','name','LC/{tag}/001'), odoo_hard_text_sql('stock_landed_cost','description','Auto LC {tag}'), 'done', v_company, v_lc_bill, v_uid, v_uid);
used_cols := ARRAY['name','description','state','company_id','date','vendor_bill_id','create_uid','write_uid','create_date','write_date'];

FOR rec IN
  SELECT column_name
  FROM information_schema.columns
  WHERE table_schema='public' AND table_name='stock_landed_cost'
    AND is_nullable='NO'
    AND column_default IS NULL
    AND column_name <> 'id'
    AND NOT (column_name = ANY(used_cols))
  ORDER BY ordinal_position
LOOP
  cols := cols || ', ' || quote_ident(rec.column_name);
  vals := vals || ', ' || odoo_hard_default_sql('stock_landed_cost', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
  used_cols := array_append(used_cols, rec.column_name::text);
END LOOP;

sql := format('INSERT INTO stock_landed_cost (%s) VALUES (%s) RETURNING id', cols, vals);
EXECUTE sql INTO v_lc;
  ELSE
    UPDATE stock_landed_cost SET vendor_bill_id=v_lc_bill WHERE id=v_lc;
  END IF;

  DELETE FROM stock_valuation_adjustment_lines WHERE cost_id=v_lc;
  
DELETE FROM stock_valuation_adjustment_lines WHERE cost_id=v_lc;

-- Insert 2 lines via dynamic SQL to support json/jsonb 'name' columns and custom NOT NULL columns.
cols := 'cost_id, product_id, name, quantity, weight, volume, former_cost, additional_landed_cost, final_cost, create_uid, write_uid, create_date, write_date';
vals := format('%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now()',
               v_lc, p_rm010, odoo_hard_text_sql('stock_valuation_adjustment_lines','name','RM-010 LC'),
               10.0, 0.0, 0.0, 0.0, 60.0, 60.0, v_uid, v_uid);
used_cols := ARRAY['cost_id','product_id','name','quantity','weight','volume','former_cost','additional_landed_cost','final_cost','create_uid','write_uid','create_date','write_date'];
FOR rec IN
  SELECT column_name
  FROM information_schema.columns
  WHERE table_schema='public' AND table_name='stock_valuation_adjustment_lines'
    AND is_nullable='NO'
    AND column_default IS NULL
    AND column_name <> 'id'
    AND NOT (column_name = ANY(used_cols))
  ORDER BY ordinal_position
LOOP
  cols := cols || ', ' || quote_ident(rec.column_name);
  vals := vals || ', ' || odoo_hard_default_sql('stock_valuation_adjustment_lines', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
  used_cols := array_append(used_cols, rec.column_name::text);
END LOOP;
sql := format('INSERT INTO stock_valuation_adjustment_lines (%s) VALUES (%s)', cols, vals);
EXECUTE sql;

-- second line
cols := 'cost_id, product_id, name, quantity, weight, volume, former_cost, additional_landed_cost, final_cost, create_uid, write_uid, create_date, write_date';
vals := format('%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now()',
               v_lc, p_rm020, odoo_hard_text_sql('stock_valuation_adjustment_lines','name','RM-020 LC'),
               10.0, 0.0, 0.0, 0.0, 40.0, 40.0, v_uid, v_uid);
used_cols := ARRAY['cost_id','product_id','name','quantity','weight','volume','former_cost','additional_landed_cost','final_cost','create_uid','write_uid','create_date','write_date'];
FOR rec IN
  SELECT column_name
  FROM information_schema.columns
  WHERE table_schema='public' AND table_name='stock_valuation_adjustment_lines'
    AND is_nullable='NO'
    AND column_default IS NULL
    AND column_name <> 'id'
    AND NOT (column_name = ANY(used_cols))
  ORDER BY ordinal_position
LOOP
  cols := cols || ', ' || quote_ident(rec.column_name);
  vals := vals || ', ' || odoo_hard_default_sql('stock_valuation_adjustment_lines', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
  used_cols := array_append(used_cols, rec.column_name::text);
END LOOP;
sql := format('INSERT INTO stock_valuation_adjustment_lines (%s) VALUES (%s)', cols, vals);
EXECUTE sql;
-- Analytic evidence (robust across Odoo versions/customizations)
v_an_plan := NULL;

-- If analytic accounts require a plan_id, ensure at least one analytic plan exists
IF odoo_hard_has_col('account_analytic_account','plan_id') THEN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='account_analytic_plan') THEN
    SELECT id INTO v_an_plan FROM account_analytic_plan ORDER BY id LIMIT 1;
    IF v_an_plan IS NULL THEN
      cols := 'name, company_id, create_uid, write_uid, create_date, write_date';
      vals := format('%s, %s, %s, %s, now(), now()',
                     odoo_hard_text_sql('account_analytic_plan','name','PLAN-odoo_hard'),
                     v_company, v_uid, v_uid);
      used_cols := ARRAY['name','company_id','create_uid','write_uid','create_date','write_date'];

      FOR rec IN
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name='account_analytic_plan'
          AND is_nullable='NO'
          AND column_default IS NULL
          AND column_name <> 'id'
          AND NOT (column_name = ANY(used_cols))
        ORDER BY ordinal_position
      LOOP
        cols := cols || ', ' || quote_ident(rec.column_name);
        vals := vals || ', ' || odoo_hard_default_sql('account_analytic_plan', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
        used_cols := array_append(used_cols, rec.column_name::text);
      END LOOP;

      sql := format('INSERT INTO account_analytic_plan (%s) VALUES (%s) RETURNING id', cols, vals);
      EXECUTE sql INTO v_an_plan;
    END IF;
  END IF;
END IF;

IF NOT EXISTS (SELECT 1 FROM account_analytic_account WHERE name::text ILIKE '%AN-odoo_hard%') THEN
  cols := 'name, company_id, create_uid, write_uid, create_date, write_date';
  vals := format('%s, %s, %s, %s, now(), now()',
                 odoo_hard_text_sql('account_analytic_account','name','AN-odoo_hard'), v_company, v_uid, v_uid);
  used_cols := ARRAY['name','company_id','create_uid','write_uid','create_date','write_date'];

  IF odoo_hard_has_col('account_analytic_account','plan_id') THEN
    cols := cols || ', plan_id';
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='account_analytic_plan') THEN
      vals := vals || format(', %s', COALESCE(v_an_plan, (SELECT id FROM account_analytic_plan ORDER BY id LIMIT 1)));
    ELSE
      vals := vals || ', 1';
    END IF;
    used_cols := array_append(used_cols,'plan_id');
  END IF;

  FOR rec IN
    SELECT column_name
    FROM information_schema.columns
    WHERE table_schema='public' AND table_name='account_analytic_account'
      AND is_nullable='NO'
      AND column_default IS NULL
      AND column_name <> 'id'
      AND NOT (column_name = ANY(used_cols))
    ORDER BY ordinal_position
  LOOP
    cols := cols || ', ' || quote_ident(rec.column_name);
    vals := vals || ', ' || odoo_hard_default_sql('account_analytic_account', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
    used_cols := array_append(used_cols, rec.column_name::text);
  END LOOP;

  sql := format('INSERT INTO account_analytic_account (%s) VALUES (%s)', cols, vals);
  EXECUTE sql;
END IF;
IF NOT EXISTS (
  SELECT 1
  FROM account_analytic_line aal
  JOIN account_analytic_account aaa ON aaa.id=aal.account_id
  WHERE aaa.name::text ILIKE '%AN-odoo_hard%'
    AND aal.name::text ILIKE '%Travel to Berlin%'
    AND ABS(aal.amount + 123.45) < 0.01
) THEN
  cols := 'name, amount, unit_amount, date, create_uid, write_uid, create_date, write_date';
  vals := format('%s, %s, %s, current_date, %s, %s, now(), now()',
                 odoo_hard_text_sql('account_analytic_line','name','Travel to Berlin'),
                 -123.45, 1.0, v_uid, v_uid);
  used_cols := ARRAY['name','amount','unit_amount','date','create_uid','write_uid','create_date','write_date'];

  -- account_id / company_id may vary across schemas
  IF odoo_hard_has_col('account_analytic_line','account_id') THEN
    cols := cols || ', account_id';
    vals := vals || format(', (SELECT id FROM account_analytic_account WHERE name::text ILIKE %L ORDER BY id DESC LIMIT 1)', '%AN-odoo_hard%');
    used_cols := array_append(used_cols,'account_id');
  ELSIF odoo_hard_has_col('account_analytic_line','analytic_account_id') THEN
    cols := cols || ', analytic_account_id';
    vals := vals || format(', (SELECT id FROM account_analytic_account WHERE name::text ILIKE %L ORDER BY id DESC LIMIT 1)', '%AN-odoo_hard%');
    used_cols := array_append(used_cols,'analytic_account_id');
  END IF;

  IF odoo_hard_has_col('account_analytic_line','company_id') THEN
    cols := cols || ', company_id';
    vals := vals || format(', %s', v_company);
    used_cols := array_append(used_cols,'company_id');
  END IF;

  IF odoo_hard_has_col('account_analytic_line','plan_id') THEN
    cols := cols || ', plan_id';
    IF v_an_plan IS NOT NULL THEN
      vals := vals || format(', %s', v_an_plan);
    ELSIF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='account_analytic_plan') THEN
      vals := vals || ', (SELECT id FROM account_analytic_plan ORDER BY id LIMIT 1)';
    ELSE
      vals := vals || ', 1';
    END IF;
    used_cols := array_append(used_cols,'plan_id');
  END IF;

  FOR rec IN
    SELECT column_name
    FROM information_schema.columns
    WHERE table_schema='public' AND table_name='account_analytic_line'
      AND is_nullable='NO'
      AND column_default IS NULL
      AND column_name <> 'id'
      AND NOT (column_name = ANY(used_cols))
    ORDER BY ordinal_position
  LOOP
    cols := cols || ', ' || quote_ident(rec.column_name);
    vals := vals || ', ' || odoo_hard_default_sql('account_analytic_line', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
    used_cols := array_append(used_cols, rec.column_name::text);
  END LOOP;

  sql := format('INSERT INTO account_analytic_line (%s) VALUES (%s)', cols, vals);
  EXECUTE sql;
END IF;

-- Stock distribution (Option 2)
  DELETE FROM stock_quant sq USING stock_location loc
  WHERE sq.location_id=loc.id AND sq.product_id IN (p_rm010, p_rm020)
    AND (loc.id=v_loc_wh OR loc.parent_path LIKE ('%/'||v_loc_wh::text||'/%'));

  DELETE FROM stock_quant sq USING stock_location loc
  WHERE sq.location_id=loc.id AND sq.product_id IN (p_rm010, p_rm020)
    AND (loc.id=v_loc_my OR loc.parent_path LIKE ('%/'||v_loc_my::text||'/%'));

-- stock_quant (RM-010 / RM-020): schema-robust insert + auto-fill NOT NULL (e.g., in_date)
-- RM-010 qty 4
cols := 'company_id, product_id, location_id, quantity, reserved_quantity, create_uid, write_uid, create_date, write_date';
vals := format('%s, %s, %s, %s, %s, %s, %s, now(), now()',
               v_company, p_rm010, v_loc_my, 4.0, 0.0, v_uid, v_uid);
used_cols := ARRAY['company_id','product_id','location_id','quantity','reserved_quantity','create_uid','write_uid','create_date','write_date'];

FOR rec IN
  SELECT column_name
  FROM information_schema.columns
  WHERE table_schema='public' AND table_name='stock_quant'
    AND is_nullable='NO'
    AND column_default IS NULL
    AND column_name <> 'id'
    AND NOT (column_name = ANY(used_cols))
  ORDER BY ordinal_position
LOOP
  cols := cols || ', ' || quote_ident(rec.column_name);
  vals := vals || ', ' || odoo_hard_default_sql('stock_quant', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
  used_cols := array_append(used_cols, rec.column_name::text);
END LOOP;

sql := format('INSERT INTO stock_quant (%s) VALUES (%s)', cols, vals);
EXECUTE sql;

-- RM-020 qty 7
cols := 'company_id, product_id, location_id, quantity, reserved_quantity, create_uid, write_uid, create_date, write_date';
vals := format('%s, %s, %s, %s, %s, %s, %s, now(), now()',
               v_company, p_rm020, v_loc_my, 7.0, 0.0, v_uid, v_uid);
used_cols := ARRAY['company_id','product_id','location_id','quantity','reserved_quantity','create_uid','write_uid','create_date','write_date'];

FOR rec IN
  SELECT column_name
  FROM information_schema.columns
  WHERE table_schema='public' AND table_name='stock_quant'
    AND is_nullable='NO'
    AND column_default IS NULL
    AND column_name <> 'id'
    AND NOT (column_name = ANY(used_cols))
  ORDER BY ordinal_position
LOOP
  cols := cols || ', ' || quote_ident(rec.column_name);
  vals := vals || ', ' || odoo_hard_default_sql('stock_quant', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
  used_cols := array_append(used_cols, rec.column_name::text);
END LOOP;

sql := format('INSERT INTO stock_quant (%s) VALUES (%s)', cols, vals);
EXECUTE sql;
  DELETE FROM stock_quant sq USING stock_location loc
  WHERE sq.location_id=loc.id AND sq.lot_id=lot_ssk2
    AND (
      loc.id=v_loc_wh OR loc.parent_path LIKE ('%/'||v_loc_wh::text||'/%')
      OR loc.id=v_loc_my OR loc.parent_path LIKE ('%/'||v_loc_my::text||'/%')
    );

-- stock_quant (finished product with lot): schema-robust insert + auto-fill NOT NULL (e.g., in_date)
cols := 'company_id, product_id, location_id, lot_id, quantity, reserved_quantity, create_uid, write_uid, create_date, write_date';
vals := format('%s, %s, %s, %s, %s, %s, %s, %s, now(), now()',
               v_company, p_fp, v_loc_my, lot_ssk2, 1.0, 0.0, v_uid, v_uid);
used_cols := ARRAY['company_id','product_id','location_id','lot_id','quantity','reserved_quantity','create_uid','write_uid','create_date','write_date'];

FOR rec IN
  SELECT column_name
  FROM information_schema.columns
  WHERE table_schema='public' AND table_name='stock_quant'
    AND is_nullable='NO'
    AND column_default IS NULL
    AND column_name <> 'id'
    AND NOT (column_name = ANY(used_cols))
  ORDER BY ordinal_position
LOOP
  cols := cols || ', ' || quote_ident(rec.column_name);
  vals := vals || ', ' || odoo_hard_default_sql('stock_quant', rec.column_name::text, v_uid, v_company, v_uom, 1, 'none');
  used_cols := array_append(used_cols, rec.column_name::text);
END LOOP;

sql := format('INSERT INTO stock_quant (%s) VALUES (%s)', cols, vals);
EXECUTE sql;
END $$;
"""

    print("[1/2] Building ground truth state...")
    run_psql(build_sql, host=args.host, port=args.port, user=args.user, db=args.db, password=args.password)
    print("OK: ground truth state inserted/updated.")

    if args.self_check:
        # Detect schema variations so self-check matches the judger across Odoo versions/customizations
        schema_sql = r"""
SELECT json_build_object(
  'sml_qty_col',
    CASE
      WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='stock_move_line' AND column_name='qty_done') THEN 'qty_done'
      WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='stock_move_line' AND column_name='quantity') THEN 'quantity'
      WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='stock_move_line' AND column_name='product_uom_qty') THEN 'product_uom_qty'
      ELSE NULL
    END,
  'pol_qty_col',
    CASE
      WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='purchase_order_line' AND column_name='product_qty') THEN 'product_qty'
      WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='purchase_order_line' AND column_name='product_uom_qty') THEN 'product_uom_qty'
      ELSE NULL
    END,
  'scrap_qty_col',
    CASE
      WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='stock_scrap' AND column_name='scrap_qty') THEN 'scrap_qty'
      WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='stock_scrap' AND column_name='product_qty') THEN 'product_qty'
      WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='stock_scrap' AND column_name='product_uom_qty') THEN 'product_uom_qty'
      ELSE NULL
    END
)::text;
"""
        schema_out = run_psql(schema_sql, host=args.host, port=args.port, user=args.user, db=args.db, password=args.password, on_error_stop=True)
        schema_lines = [ln.strip() for ln in schema_out.splitlines() if ln.strip()]
        schema = {}
        if schema_lines:
            try:
                schema = json.loads(schema_lines[-1])
            except Exception:
                schema = {}

        sml_qty_col = schema.get("sml_qty_col") or "qty_done"
        if sml_qty_col not in ("qty_done", "quantity", "product_uom_qty"):
            sml_qty_col = "qty_done"

        pol_qty_col = schema.get("pol_qty_col") or "product_qty"
        if pol_qty_col not in ("product_qty", "product_uom_qty"):
            pol_qty_col = "product_qty"

        scrap_qty_col = schema.get("scrap_qty_col") or "scrap_qty"
        if scrap_qty_col not in ("scrap_qty", "product_qty", "product_uom_qty"):
            scrap_qty_col = "scrap_qty"

        evidence_sql = f"""
WITH
prod AS (
  SELECT pp.id AS product_id, pt.default_code
  FROM product_product pp
  JOIN product_template pt ON pt.id = pp.product_tmpl_id
  WHERE pt.default_code IN ('FP-1000','SA-200','SC-300','RM-010','RM-020','RM-030','RM-040','ACC-900')
),
eur AS (SELECT id AS currency_id FROM res_currency WHERE name='EUR' LIMIT 1),

lc AS (
  SELECT slc.id
  FROM stock_landed_cost slc
  JOIN stock_valuation_adjustment_lines sval ON sval.cost_id = slc.id
  JOIN prod p ON p.product_id = sval.product_id
  WHERE p.default_code IN ('RM-010','RM-020')
    AND (COALESCE(slc.name::text,'') ILIKE '%{tag}%' OR COALESCE(slc.description::text,'') ILIKE '%{tag}%')
  GROUP BY slc.id
  HAVING ABS(SUM(sval.additional_landed_cost) - 100.0) < 0.0001
  ORDER BY slc.id DESC
  LIMIT 1
),
lc_split AS (
  SELECT p.default_code, ROUND(SUM(sval.additional_landed_cost)::numeric, 2) AS add_cost
  FROM stock_valuation_adjustment_lines sval
  JOIN lc ON lc.id = sval.cost_id
  JOIN prod p ON p.product_id = sval.product_id
  GROUP BY p.default_code
),
lc_bill_paid AS (
  SELECT COUNT(*) AS cnt
  FROM stock_landed_cost slc
  JOIN lc ON lc.id = slc.id
  JOIN account_move am ON am.id = slc.vendor_bill_id
  WHERE am.move_type='in_invoice' AND am.state='posted' AND am.payment_state='paid'
),

so_candidate AS (
  SELECT so.id, so.name,
         SUM(CASE WHEN pt.default_code='FP-1000' THEN sol.product_uom_qty ELSE 0 END) AS fp_qty,
         SUM(CASE WHEN pt.default_code='ACC-900' THEN sol.product_uom_qty ELSE 0 END) AS acc_qty,
         BOOL_OR(
           COALESCE(so.client_order_ref::text,'') ILIKE '%{tag}%'
           OR COALESCE(so.origin::text,'') ILIKE '%{tag}%'
           OR COALESCE(so.note::text,'') ILIKE '%{tag}%'
           OR COALESCE(so.name::text,'') ILIKE '%{tag}%'
         ) AS has_tag,
         MAX(so.state) AS state
  FROM sale_order so
  JOIN sale_order_line sol ON sol.order_id = so.id
  JOIN product_product pp ON pp.id = sol.product_id
  JOIN product_template pt ON pt.id = pp.product_tmpl_id
  WHERE pt.default_code IN ('FP-1000','ACC-900')
  GROUP BY so.id, so.name
),
so AS (
  SELECT *
  FROM so_candidate
  WHERE has_tag
  ORDER BY id DESC
  LIMIT 1
),

dropship_done AS (
  SELECT COUNT(*) AS cnt
  FROM stock_move_line sml
  JOIN stock_picking sp ON sp.id = sml.picking_id
  JOIN stock_picking_type spt ON spt.id = sp.picking_type_id
  JOIN product_product pp ON pp.id = sml.product_id
  JOIN product_template pt ON pt.id = pp.product_tmpl_id
  WHERE spt.code='dropship' AND sp.state='done'
    AND pt.default_code='ACC-900'
  GROUP BY sp.id
  HAVING ABS(SUM(sml.{sml_qty_col}) - 3.0) < 0.0001
),

mo_sa_done AS (
  SELECT COUNT(*) AS cnt
  FROM mrp_production mp
  JOIN product_product pp ON pp.id = mp.product_id
  JOIN product_template pt ON pt.id = pp.product_tmpl_id
  WHERE pt.default_code='SA-200' AND mp.state='done' AND ABS(mp.product_qty - 3.0) < 0.0001
),
mo_fp_done AS (
  SELECT COUNT(*) AS cnt
  FROM mrp_production mp
  JOIN product_product pp ON pp.id = mp.product_id
  JOIN product_template pt ON pt.id = pp.product_tmpl_id
  WHERE pt.default_code='FP-1000' AND mp.state='done' AND ABS(mp.product_qty - 3.0) < 0.0001
),
workorders_done AS (
  SELECT COUNT(*) AS cnt
  FROM mrp_workorder wo
  WHERE wo.state='done'
),

paid_invoices AS (
  SELECT ROUND(am.amount_total::numeric, 2) AS amount_total
  FROM account_move am
  JOIN eur ON eur.currency_id = am.currency_id
  WHERE am.move_type = 'out_invoice'
    AND am.state = 'posted'
    AND am.payment_state = 'paid'
    AND (
      COALESCE(am.ref::text,'') ILIKE '%{tag}%'
      OR COALESCE(am.invoice_origin::text,'') ILIKE '%{tag}%'
      OR COALESCE(am.narration::text,'') ILIKE '%{tag}%'
    )
),
paid_credit AS (
  SELECT ROUND(am.amount_total::numeric, 2) AS amount_total
  FROM account_move am
  JOIN eur ON eur.currency_id = am.currency_id
  WHERE am.move_type = 'out_refund'
    AND am.state = 'posted'
    AND am.payment_state = 'paid'
    AND (
      COALESCE(am.ref::text,'') ILIKE '%{tag}%'
      OR COALESCE(am.invoice_origin::text,'') ILIKE '%{tag}%'
      OR COALESCE(am.narration::text,'') ILIKE '%{tag}%'
    )
),

paid_vendor_bills AS (
  SELECT COUNT(*) AS cnt
  FROM account_move am
  WHERE am.move_type='in_invoice'
    AND am.state='posted'
    AND am.payment_state='paid'
    AND (
      COALESCE(am.ref::text,'') ILIKE '%{tag}%'
      OR COALESCE(am.invoice_origin::text,'') ILIKE '%{tag}%'
      OR COALESCE(am.narration::text,'') ILIKE '%{tag}%'
    )
),

fp_lots AS (
  SELECT sl.name
  FROM stock_lot sl
  JOIN prod p ON p.product_id = sl.product_id
  WHERE p.default_code='FP-1000' AND sl.name IN ('SSK-0001','SSK-0002','SSK-0003')
),
sc_lots AS (
  SELECT sl.name
  FROM stock_lot sl
  JOIN prod p ON p.product_id = sl.product_id
  WHERE p.default_code='SC-300' AND sl.name IN ('CS-0001','CS-0002','CS-0003')
),

subcontract_sc_done AS (
  SELECT COUNT(DISTINCT sl.name) AS cnt
  FROM stock_move_line sml
  JOIN stock_picking sp ON sp.id = sml.picking_id
  JOIN stock_lot sl ON sl.id = sml.lot_id
  JOIN product_product pp ON pp.id = sml.product_id
  JOIN product_template pt ON pt.id = pp.product_tmpl_id
  WHERE sp.state='done' AND pt.default_code='SC-300' AND sl.name IN ('CS-0001','CS-0002','CS-0003')
),

wh_base AS (
  SELECT sw.code, sw.lot_stock_id
  FROM stock_warehouse sw
  WHERE sw.code IN ('WH','My Co')
),
wh_locs AS (
  SELECT wb.code AS wh_code, loc.id AS location_id
  FROM wh_base wb
  JOIN stock_location loc
    ON loc.id = wb.lot_stock_id
    OR loc.parent_path LIKE CONCAT('%/', wb.lot_stock_id::text, '/%')
),
wh_qty AS (
  SELECT
    wl.wh_code,
    p.default_code AS product_code,
    ROUND(SUM(sq.quantity)::numeric, 6) AS qty
  FROM stock_quant sq
  JOIN wh_locs wl ON wl.location_id = sq.location_id
  JOIN prod p ON p.product_id = sq.product_id
  GROUP BY wl.wh_code, p.default_code
),
ssk2_wh AS (
  SELECT
    wl.wh_code,
    ROUND(SUM(sq.quantity)::numeric, 6) AS qty
  FROM stock_quant sq
  JOIN stock_lot sl ON sl.id = sq.lot_id
  JOIN wh_locs wl ON wl.location_id = sq.location_id
  WHERE sl.name='SSK-0002'
  GROUP BY wl.wh_code
),

scrap_rm030 AS (
  SELECT COUNT(*) AS cnt
  FROM stock_scrap ss
  JOIN prod p ON p.product_id = ss.product_id
  WHERE p.default_code='RM-030'
    AND ss.state='done'
    AND ABS(ss.{scrap_qty_col} - 1.0) < 0.0001
),
extra_po_rm030 AS (
  SELECT COUNT(*) AS cnt
  FROM purchase_order po
  JOIN purchase_order_line pol ON pol.order_id = po.id
  JOIN prod p ON p.product_id = pol.product_id
  WHERE p.default_code='RM-030'
    AND ABS(pol.{pol_qty_col} - 1.0) < 0.0001
    AND (
      COALESCE(po.partner_ref::text,'') ILIKE '%{tag}%'
      OR COALESCE(po.origin::text,'') ILIKE '%{tag}%'
      OR COALESCE(po.name::text,'') ILIKE '%{tag}%'
    )
),

analytic_expense AS (
  SELECT COUNT(*) AS cnt
  FROM account_analytic_line aal
  JOIN account_analytic_account aaa ON aaa.id = aal.account_id
  WHERE aaa.name::text ILIKE '%AN-odoo_hard%'
    AND aal.name::text ILIKE '%Travel to Berlin%'
    AND ABS(aal.amount + 123.45) < 0.01
)

SELECT json_build_object(
  'lc_split', (SELECT COALESCE(json_object_agg(default_code, add_cost), '{{}}'::json) FROM lc_split),
  'lc_bill_paid_cnt', (SELECT cnt FROM lc_bill_paid),
  'so', (SELECT COALESCE(json_build_object('id', id, 'name', name, 'fp_qty', fp_qty, 'acc_qty', acc_qty, 'state', state), '{{}}'::json) FROM so),
  'dropship_done_cnt', (SELECT COALESCE(SUM(cnt), 0) FROM dropship_done),
  'mo_sa_done_cnt', (SELECT cnt FROM mo_sa_done),
  'mo_fp_done_cnt', (SELECT cnt FROM mo_fp_done),
  'workorders_done_cnt', (SELECT cnt FROM workorders_done),
  'paid_invoices', (SELECT COALESCE(json_agg(amount_total ORDER BY amount_total), '[]'::json) FROM paid_invoices),
  'paid_credit', (SELECT COALESCE(json_agg(amount_total ORDER BY amount_total), '[]'::json) FROM paid_credit),
  'paid_vendor_bills_cnt', (SELECT cnt FROM paid_vendor_bills),
  'fp_lots', (SELECT COALESCE(json_agg(name ORDER BY name), '[]'::json) FROM fp_lots),
  'sc_lots', (SELECT COALESCE(json_agg(name ORDER BY name), '[]'::json) FROM sc_lots),
  'subcontract_sc_distinct_lots', (SELECT cnt FROM subcontract_sc_done),
  'wh_qty', (SELECT COALESCE(json_agg(json_build_object('wh', wh_code, 'product', product_code, 'qty', qty) ORDER BY wh_code, product_code), '[]'::json) FROM wh_qty),
  'ssk2_wh', (SELECT COALESCE(json_agg(json_build_object('wh', wh_code, 'qty', qty) ORDER BY wh_code), '[]'::json) FROM ssk2_wh),
  'scrap_rm030_cnt', (SELECT cnt FROM scrap_rm030),
  'extra_po_rm030_cnt', (SELECT cnt FROM extra_po_rm030),
  'analytic_expense_cnt', (SELECT cnt FROM analytic_expense)
)::text;
"""
        print("[2/2] Self-check evidence JSON:")
        out = run_psql(evidence_sql, host=args.host, port=args.port, user=args.user, db=args.db, password=args.password)
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        print(lines[-1] if lines else out)

    print("DONE.")


if __name__ == "__main__":
    main()
