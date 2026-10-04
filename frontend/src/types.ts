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

export interface ClarificationOptionView {
  option_id: number;
  text: string;
}

export interface ClarificationView {
  question_id: string;
  question_text: string;
  options: ClarificationOptionView[];
  expires_at?: number | null;
  status?: string;
}

export interface ClarificationResponse {
  question_id: string;
  option_id: number;
}

export interface ReplaceDishTarget {
  target_recipe_id: number;
  dish_name: string;
  source_plan_id: string;
  source_menu_hash: string;
}

export interface CreateRequestPayload {
  idempotency_key: string;
  participants: AnonymousParticipant[];
  message: string;
  session_id: string;
  clarification_response?: ClarificationResponse;
  action?: "recommend" | "replace_dish";
  target_recipe_id?: number;
  source_plan_id?: string;
  source_menu_hash?: string;
}

export interface SessionState {
  session_id: string;
  participant_refs: string[];
  request_count: number;
  last_request_at?: string | null;
  current_menu: PublicMenuSummary | null;
  active_clarification?: ClarificationView | null;
}

export interface RequestState {
  request_id: string;
  session_id: string;
  status: string;
  created_at: string;
  updated_at?: string;
  result_summary?: ResultSummary | null;
  error?: { code?: string; message?: string } | null;
  active_clarification?: ClarificationView | null;
}

export interface PhaseEvent {
  id: string;
  event: string;
  data: Record<string, unknown>;
}

export interface ThoughtStep {
  node_id: string;
  execution_generation?: number;
  invocation_id?: string;
  title: string;
  status: "running" | "done" | "warning" | "error";
  summary?: string;
  tool_name?: string;
  duration_ms?: number;
}

export interface ChatMessage {
  id: string;
  requestId?: string;
  role: "user" | "assistant";
  content: string;
  createdAt: number;
  status: "sending" | "complete" | "error" | "cancelled";
  isCommitted?: boolean;
  isAnswered?: boolean;
  thoughts?: ThoughtStep[];
  clarification?: ClarificationView;
  selectedOptionId?: number;
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
