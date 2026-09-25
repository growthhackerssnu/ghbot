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
