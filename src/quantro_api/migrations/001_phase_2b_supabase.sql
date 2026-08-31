create extension if not exists pgcrypto;

create table if not exists quantro_users (
    auth_user_id uuid primary key references auth.users(id) on delete cascade,
    email text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists quantro_accounts (
    id uuid primary key,
    name text not null,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null,
    updated_at timestamptz not null
);

create table if not exists quantro_user_accounts (
    auth_user_id uuid primary key references quantro_users(auth_user_id) on delete cascade,
    account_id uuid not null unique references quantro_accounts(id) on delete cascade,
    created_at timestamptz not null default now()
);

create table if not exists quantro_balances (
    account_id uuid not null references quantro_accounts(id) on delete cascade,
    asset text not null,
    free numeric(38, 18) not null default 0,
    locked numeric(38, 18) not null default 0,
    total numeric(38, 18) generated always as (free + locked) stored,
    updated_at timestamptz not null default now(),
    primary key (account_id, asset),
    check (free >= 0),
    check (locked >= 0)
);

create table if not exists quantro_orders (
    id uuid primary key,
    account_id uuid not null references quantro_accounts(id) on delete cascade,
    symbol text not null,
    side text not null,
    order_type text not null,
    status text not null,
    time_in_force text not null,
    quantity numeric(38, 18) not null,
    price numeric(38, 18) not null,
    stop_price numeric(38, 18) not null,
    filled_quantity numeric(38, 18) not null,
    remaining_quantity numeric(38, 18) not null,
    average_fill_price numeric(38, 18) not null,
    total_fees numeric(38, 18) not null,
    client_order_id text not null default '',
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null,
    updated_at timestamptz not null,
    expires_at timestamptz
);

create index if not exists quantro_orders_account_id_idx on quantro_orders(account_id);
create index if not exists quantro_orders_status_idx on quantro_orders(status);

create table if not exists quantro_trades (
    id uuid primary key,
    order_id uuid not null references quantro_orders(id) on delete cascade,
    account_id uuid not null references quantro_accounts(id) on delete cascade,
    symbol text not null,
    side text not null,
    quantity numeric(38, 18) not null,
    price numeric(38, 18) not null,
    notional numeric(38, 18) not null,
    fee numeric(38, 18) not null,
    fee_asset text not null,
    is_maker boolean not null,
    metadata jsonb not null default '{}'::jsonb,
    executed_at timestamptz not null
);

create index if not exists quantro_trades_account_id_idx on quantro_trades(account_id);
create index if not exists quantro_trades_order_id_idx on quantro_trades(order_id);

create table if not exists quantro_positions (
    id uuid primary key,
    account_id uuid not null references quantro_accounts(id) on delete cascade,
    symbol text not null,
    side text not null,
    size numeric(38, 18) not null,
    entry_price numeric(38, 18) not null,
    mark_price numeric(38, 18) not null,
    unrealized_pnl numeric(38, 18) not null,
    realized_pnl numeric(38, 18) not null,
    leverage numeric(38, 18) not null,
    liquidation_price numeric(38, 18) not null,
    metadata jsonb not null default '{}'::jsonb,
    opened_at timestamptz not null,
    updated_at timestamptz not null,
    unique (account_id, symbol)
);

create table if not exists quantro_pnl_state (
    account_id uuid primary key references quantro_accounts(id) on delete cascade,
    total_unrealized_pnl numeric(38, 18) not null default 0,
    total_realized_pnl numeric(38, 18) not null default 0,
    total_pnl numeric(38, 18) generated always as (total_unrealized_pnl + total_realized_pnl) stored,
    sequence bigint not null default 0,
    updated_at timestamptz not null default now()
);

create table if not exists quantro_engine_state (
    id boolean primary key default true check (id),
    state jsonb not null,
    updated_at timestamptz not null default now()
);
