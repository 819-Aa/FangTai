<script setup lang="ts">
import { ref } from "vue";
import {
  ArrowUp,
  Bot,
  BrainCircuit,
  CheckCircle2,
  LoaderCircle,
  Sparkles,
  UserRound,
} from "@lucide/vue";
import type { ChatMessage, PhaseEvent, UserProfile } from "@/types";

const props = defineProps<{
  messages: ChatMessage[];
  phases: PhaseEvent[];
  answer: string;
  status: string;
  isStreaming: boolean;
  canSend: boolean;
  participants: UserProfile[];
}>();
const emit = defineEmits<{ send: [message: string] }>();

const draft = ref("");
const suggestions = [
  "推荐三菜一汤，家常口味，45分钟内完成",
  "清淡一点，不要海鲜",
  "适合高血压患者的晚餐",
  "两人份，暖胃的家常菜",
];

function send(value = draft.value) {
  const v = value.trim();
  if (!v || props.isStreaming || !props.canSend) return;
  draft.value = "";
  emit("send", v);
}
function onKeydown(e: KeyboardEvent) {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
}

function phaseLabel(e: PhaseEvent): string {
  const d = e.data as { stage?: string; summary?: string };
  return d.summary || d.stage || e.event;
}
</script>

<template>
  <main class="chat-panel">
    <div class="message-list">
      <section v-if="!messages.length" class="conversation-empty">
        <div class="empty-symbol"><Sparkles :size="26" /></div>
        <h2>今天想吃什么？</h2>
        <p>先在上方选择健康档案，再描述口味、忌口或用餐场景。</p>
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
            <p v-if="m.status === 'error'" class="error-text">{{ m.content }}</p>
          </template>
        </div>
      </article>
    </div>

    <div v-if="phases.length" class="phase-panel">
      <div class="phase-head">
        <BrainCircuit :size="14" />
        <span>执行阶段</span>
        <span v-if="isStreaming" class="phase-running"><LoaderCircle class="spin" :size="12" />运行中</span>
        <span v-else-if="status" class="phase-done"><CheckCircle2 :size="12" />{{ status }}</span>
      </div>
      <ol class="phase-list">
        <li v-for="(p, i) in phases" :key="i">
          <span class="phase-tag">{{ p.event }}</span>
          <span class="phase-text">{{ phaseLabel(p) }}</span>
        </li>
      </ol>
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
