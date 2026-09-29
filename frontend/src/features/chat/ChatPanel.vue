<script setup lang="ts">
import { computed, ref } from "vue";
import {
  ArrowUp,
  Bot,
  BrainCircuit,
  CheckCircle2,
  LoaderCircle,
  Sparkles,
  UserRound,
} from "@lucide/vue";
import { terminalLabel } from "@/stores/recommendation";
import type {
  AnonymousParticipant,
  ChatMessage,
  ClarificationView,
  PhaseEvent,
  PublicMenuSummary,
} from "@/types";

const props = defineProps<{
  messages: ChatMessage[];
  phases: PhaseEvent[];
  answer: string;
  clarification: string;
  activeClarification?: ClarificationView | null;
  status: string;
  isStreaming: boolean;
  canSend: boolean;
  participants: AnonymousParticipant[];
  currentMenu: PublicMenuSummary | null;
}>();
const emit = defineEmits<{
  send: [message: string];
  selectOption: [optionId: number];
}>();

const draft = ref("");
const suggestions = [
  "推荐三菜一汤，家常口味，45分钟内完成",
  "清淡一点，不要海鲜",
  "适合高血压患者的晚餐",
  "两人份，暖胃的家常菜",
];

const statusLabel = computed(() => terminalLabel(props.status));
// answer_ready 仅“待最终确认”；result_committed 才视为已完成
const answered = computed(() =>
  props.phases.some((p) => p.event === "answer_ready"),
);
const committed = computed(() =>
  props.phases.some((p) => p.event === "result_committed"),
);
const needsClarification = computed(() =>
  props.status === "needs_clarification" || props.clarification !== "",
);

function send(value = draft.value) {
  const v = value.trim();
  if (!v || props.isStreaming || !props.canSend) return;
  draft.value = "";
  emit("send", v);
}
function onKeydown(e: KeyboardEvent) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send();
  }
}

function phaseLabel(e: PhaseEvent): string {
  const d = e.data as { stage?: string; summary?: string };
  return d.summary || d.stage || e.event;
}

function phaseBadge(e: PhaseEvent): string {
  if (e.event === "answer_ready") return "待最终确认";
  if (e.event === "result_committed") return "已完成";
  return "";
}
</script>

<template>
  <main class="chat-panel">
    <div class="message-list">
      <section v-if="!messages.length" class="conversation-empty">
        <div class="empty-symbol"><Sparkles :size="26" /></div>
        <h2>今天想吃什么？</h2>
        <p>先在上方添加匿名成员，再描述口味、忌口或用餐场景。</p>
        <div class="suggestion-grid">
          <button
            v-for="s in suggestions"
            :key="s"
            type="button"
            :disabled="!canSend"
            @click="send(s)"
          >{{ s }}</button>
        </div>
      </section>

      <article
        v-for="m in messages"
        :key="m.id"
        class="message"
        :class="[m.role, m.status]"
      >
        <div class="avatar">
          <UserRound v-if="m.role === 'user'" :size="16" />
          <Bot v-else :size="16" />
        </div>
        <div class="bubble">
          <template v-if="m.role === 'user'">{{ m.content }}</template>
          <template v-else>
            <p v-if="m.status === 'sending' && !m.content" class="thinking">
              <LoaderCircle class="spin" :size="14" /> 正在分析…
            </p>
            <p v-if="m.content" class="answer-text">{{ m.content }}</p>
            <p v-if="committed" class="answer-badge">
              <CheckCircle2 :size="13" /> 菜单已生成
            </p>
            <p v-else-if="answered" class="answer-badge pending">
              <LoaderCircle class="spin" :size="13" /> 待最终确认
            </p>
            <p v-if="m.status === 'error'" class="error-text">{{ m.content }}</p>
          </template>
        </div>
      </article>

      <section
        v-if="currentMenu"
        class="committed-menu"
        data-testid="committed-menu"
        aria-label="已提交菜单"
      >
        <div class="committed-menu-title">
          <CheckCircle2 :size="15" /> 已提交菜单
        </div>
        <ol>
          <li v-for="item in currentMenu.items" :key="item.recipe_id">
            <span>{{ item.name }}</span>
            <small>#{{ item.recipe_id }}</small>
          </li>
        </ol>
      </section>
    </div>

    <div v-if="phases.length" class="phase-panel">
      <div class="phase-head">
        <BrainCircuit :size="14" />
        <span>执行阶段</span>
        <span v-if="isStreaming" class="phase-running">
          <LoaderCircle class="spin" :size="12" />运行中
        </span>
        <span v-else-if="status" class="phase-done">
          <CheckCircle2 :size="12" />{{ statusLabel }}
        </span>
      </div>
      <ol class="phase-list">
        <li v-for="(p, i) in phases" :key="p.id || i">
          <span class="phase-tag">{{ p.event }}</span>
          <span class="phase-text">{{ phaseLabel(p) }}</span>
          <span v-if="phaseBadge(p)" class="phase-badge">{{ phaseBadge(p) }}</span>
        </li>
      </ol>
    </div>

    <div
      v-if="props.activeClarification && props.activeClarification.options && props.activeClarification.options.length"
      class="clarification-panel"
      role="region"
      aria-label="澄清选项"
    >
      <div class="clarification-title">
        <span class="clarification-label">待确认</span>
        <span class="clarification-text">{{ props.activeClarification.question_text }}</span>
      </div>
      <div class="clarification-options">
        <button
          v-for="opt in props.activeClarification.options"
          :key="opt.option_id"
          type="button"
          class="clarification-option-btn"
          :disabled="props.isStreaming"
          @click="emit('selectOption', opt.option_id)"
        >
          <span class="option-num">{{ opt.option_id }}.</span> {{ opt.text }}
        </button>
      </div>
    </div>
    <div v-else-if="needsClarification" class="clarification-bar" role="status">
      <span class="clarification-label">待确认</span>
      <span class="clarification-text">{{ props.clarification }}</span>
    </div>

    <div v-if="status && !isStreaming" class="terminal-bar" :class="`term-${status}`" role="status">
      {{ statusLabel }}
    </div>

    <div class="composer">
      <textarea
        v-model="draft"
        rows="1"
        maxlength="1000"
        :disabled="isStreaming"
        placeholder="描述口味、食材、餐次或临时健康情况…"
        @keydown="onKeydown"
      />
      <button
        class="send-button"
        type="button"
        :disabled="!canSend || !draft.trim() || isStreaming"
        aria-label="发送"
        @click="send()"
      ><ArrowUp :size="19" /></button>
    </div>
  </main>
</template>
