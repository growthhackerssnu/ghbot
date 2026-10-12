-- Run this once in Supabase SQL Editor.
-- The key used by the MCP server must be allowed to read/write these tables
-- (a service_role key is simplest for a private backend).

create table if not exists public.ghbot_chunks (
    page_id text not null,
    chunk_index integer not null,
    url text,
    title text,
    source_label text not null,
    chunk_text text not null,
    embedding jsonb not null,
    last_edited text,
    primary key (page_id, chunk_index)
);

create index if not exists ghbot_chunks_source_label_idx
    on public.ghbot_chunks (source_label);

create table if not exists public.ghbot_pages (
    page_id text primary key,
    source_label text not null,
    last_edited_time text
);

create index if not exists ghbot_pages_source_label_idx
    on public.ghbot_pages (source_label);

create table if not exists public.ghbot_drive_files (
    file_id text primary key,
    team text not null
);

create index if not exists ghbot_drive_files_team_idx
    on public.ghbot_drive_files (team);

create table if not exists public.ghbot_notion_content (
    page_id text primary key,
    url text,
    title text,
    body text not null,
    last_edited_time text,
    indexed_at timestamptz not null default now()
);

create index if not exists ghbot_notion_content_title_idx
    on public.ghbot_notion_content (title);

-- These tables contain private Notion/Drive material. Do not expose them via
-- the publishable/anon key. The MCP server and sync worker use SUPABASE_KEY.
alter table public.ghbot_chunks enable row level security;
alter table public.ghbot_pages enable row level security;
alter table public.ghbot_drive_files enable row level security;
alter table public.ghbot_notion_content enable row level security;

-- ---------------------------------------------------------------------------
-- Slack. Messages are read with each member's own user token and stored once
-- per conversation. Who may search a chunk is decided at query time from the
-- asking member's live Slack membership (see ghbot_match_slack), so a DM is
-- only ever returned to the people in it.

create extension if not exists vector;

-- One row per ghbot member who connected Slack. Token is Fernet-encrypted
-- with SLACK_TOKEN_ENCRYPTION_KEY, which lives only on Cloud Run.
create table if not exists public.ghbot_slack_users (
    subject text primary key,
    slack_user_id text not null,
    team_id text not null,
    token_ciphertext text not null,
    scopes text,
    is_guest boolean not null default false,
    connected_at timestamptz not null default now(),
    revoked_at timestamptz
);

create table if not exists public.ghbot_slack_channels (
    channel_id text primary key,
    channel_type text not null,
    name text,
    synced_until double precision,
    synced_at timestamptz
);

-- A document is a thread (parent + replies) or one KST day of top-level
-- messages in a conversation. content_hash skips re-embedding unchanged docs.
create table if not exists public.ghbot_slack_docs (
    doc_id text primary key,
    channel_id text not null,
    content_hash text not null,
    ts_start double precision not null,
    ts_end double precision not null
);

create index if not exists ghbot_slack_docs_channel_idx
    on public.ghbot_slack_docs (channel_id, ts_start);

create table if not exists public.ghbot_slack_chunks (
    doc_id text not null,
    chunk_index integer not null,
    channel_id text not null,
    is_public boolean not null,
    title text,
    url text,
    chunk_text text not null,
    embedding vector(384) not null,
    ts_start double precision not null,
    ts_end double precision not null,
    primary key (doc_id, chunk_index)
);

create index if not exists ghbot_slack_chunks_channel_idx
    on public.ghbot_slack_chunks (channel_id);

alter table public.ghbot_slack_users enable row level security;
alter table public.ghbot_slack_channels enable row level security;
alter table public.ghbot_slack_docs enable row level security;
alter table public.ghbot_slack_chunks enable row level security;

-- Candidates for one member's Slack search: the nearest chunks by embedding
-- plus the chunks with the most keyword hits, restricted to conversations the
-- member is in (channel_ids, from their live users.conversations) and, for
-- non-guests, public channels. The server fuses the two rankings.
create or replace function public.ghbot_match_slack(
    query_embedding vector(384),
    channel_ids text[],
    include_public boolean,
    query_terms text[],
    match_count integer default 50,
    since double precision default null,
    until double precision default null,
    only_channels text[] default null
)
returns table (
    doc_id text,
    chunk_index integer,
    channel_id text,
    title text,
    url text,
    chunk_text text,
    ts_start double precision,
    ts_end double precision,
    semantic_score double precision,
    term_hits integer
)
language sql stable
as $$
    with allowed as (
        select c.*
        from public.ghbot_slack_chunks c
        where (c.channel_id = any(channel_ids) or (include_public and c.is_public))
          and (since is null or c.ts_end >= since)
          and (until is null or c.ts_start <= until)
          and (only_channels is null or c.channel_id = any(only_channels))
    ),
    scored as (
        select a.doc_id, a.chunk_index, a.channel_id, a.title, a.url, a.chunk_text, a.ts_start, a.ts_end,
               1 - (a.embedding <=> query_embedding) as semantic_score,
               (select count(*)::integer from unnest(query_terms) t
                 where a.chunk_text ilike '%' || t || '%' or a.title ilike '%' || t || '%') as term_hits
        from allowed a
    ),
    semantic as (select * from scored order by semantic_score desc limit match_count),
    lexical as (select * from scored where term_hits > 0 order by term_hits desc limit match_count),
    candidates as (select * from semantic union select * from lexical)
    select doc_id, chunk_index, channel_id, title, url, chunk_text, ts_start, ts_end, semantic_score, term_hits
    from candidates
$$;

revoke all on function public.ghbot_match_slack from public, anon, authenticated;
