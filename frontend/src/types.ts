// 匿名参与者槽位：只携带 participant_ref，不读取/提交真实 user_id 或健康详情
export interface AnonymousParticipant {
  participant_ref: string;
  label: string;
}

export interface PublicMenuItem {
  recipe_id: number;
  name: string;
}

export interface PublicMenuSummary {
  build_id: string;
  plan_id: string;
  menu_hash: string;
  recipe_ids: number[];
  items: PublicMenuItem[];
}

export interface ResultSummary {
  status: string;
  answer?: {
    text: string;
    menu_ref: string;
    evidence_refs: string[];
  };
  menu_summary?: PublicMenuSummary;
}

export interface SessionState {
  session_id: string;
  participant_refs: string[];
  request_count: number;
  last_request_at?: string | null;
  current_menu: PublicMenuSummary | null;
}

export interface RequestState {
  request_id: string;
  session_id: string;
  status: string;
  created_at: string;
  updated_at?: string;
  result_summary?: ResultSummary | null;
  error?: { code?: string; message?: string } | null;
}

export interface PhaseEvent {
  id: string;
  event: string;
  data: Record<string, unknown>;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt: number;
  status: "sending" | "complete" | "error";
}

// 终态独立展示（禁止统一成普通失败）
export const TERMINAL_STATUSES = [
  "completed",
  "no_safe_menu",
  "no_feasible_menu",
  "strict_time_indeterminate",
  "failed",
  "cancelled",
  "interrupted",
] as const;

export type TerminalStatus = (typeof TERMINAL_STATUSES)[number];

export const TERMINAL_SET = new Set<string>(TERMINAL_STATUSES);
