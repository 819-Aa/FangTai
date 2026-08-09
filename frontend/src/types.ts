export interface UserProfile {
  id: number;
  display_name: string;
  age?: number;
  gender?: string;
  allergies: string[];
  diseases: string[];
  special_group: string[];
}

export interface Participant {
  participant_ref: string;
  user_id: number;
  display_name: string;
}

export interface RequestState {
  request_id: string;
  session_id: string;
  status: string;
  created_at: string;
  updated_at?: string;
  result_summary?: Record<string, unknown> | null;
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
