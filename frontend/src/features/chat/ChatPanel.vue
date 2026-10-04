<script setup lang="ts">
import { computed, ref } from "vue";
import {
  ArrowUp,
  Bot,
  BrainCircuit,
  CheckCircle2,
  ChevronDown,
  LoaderCircle,
  Sparkles,
  Square,
  UserRound,
} from "@lucide/vue";
import { terminalLabel } from "@/stores/recommendation";
import type {
  AnonymousParticipant,
  ChatMessage,
  ClarificationView,
  PhaseEvent,
  PublicMenuItem,
  PublicMenuSummary,
  ReplaceDishTarget,
} from "@/types";

const props = defineProps<{
  messages: ChatMessage[];
  phases: PhaseEvent[];
  answer: string;
  clarification: string;
  activeClarification?: ClarificationView | null;
  status: string;
  isStreaming: boolean;
  isCancelling?: boolean;
  canSend: boolean;
  participants: AnonymousParticipant[];
  currentMenu: PublicMenuSummary | null;
  pendingReplaceDish?: ReplaceDishTarget | null;
}>();
const emit = defineEmits<{
  send: [message: string];
  selectOption: [optionId: number, messageId?: string];
  cancel: [];
  replaceDish: [dish: PublicMenuItem];
  cancelReplaceDish: [];
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

function onReplaceDish(item: PublicMenuItem) {
  if (props.isStreaming) return;
  if (!draft.value) {
    draft.value = `把${item.name}换掉，推荐一道别的菜`;
  }
  emit("replaceDish", item);
}

function isLatestAssistant(m: ChatMessage): boolean {
  const assistants = props.messages.filter((msg) => msg.role === "assistant");
  return assistants.length > 0 && assistants[assistants.length - 1].id === m.id;
}

function isMessageCommitted(m: ChatMessage): boolean {
  if (m.isCommitted) return true;
  if (isLatestAssistant(m) && committed.value && m.status === "complete") return true;
  return false;
}

function isMessageAnswered(m: ChatMessage): boolean {
  if (m.isCommitted) return false;
  if (m.isAnswered && m.status === "sending") return true;
  if (isLatestAssistant(m) && answered.value && !committed.value && m.status === "sending") return true;
  return false;
}

const hasMessageBoundClarification = computed(() =>
  props.messages.some(
    (m) =>
      m.role === "assistant" &&
      m.clarification &&
      props.activeClarification &&
      m.clarification.question_id === props.activeClarification.question_id,
  ),
);

function isClarificationActive(m: ChatMessage): boolean {
  if (!m.clarification || !props.activeClarification) return false;
  return props.activeClarification.question_id === m.clarification.question_id;
}

function clarificationStatusText(m: ChatMessage): string {
  if (m.selectedOptionId) return "已确认";
  if (isClarificationActive(m)) return "待确认";
  return "已失效";
}

function canClickClarificationOption(m: ChatMessage): boolean {
  if (m.selectedOptionId) return false;
  if (!isClarificationActive(m)) return false;
  if (props.isStreaming || props.isCancelling) return false;
  return true;
}

const expandedThoughts = ref<Record<string, boolean>>({});
function toggleThoughts(messageId: string) {
  expandedThoughts.value[messageId] = !isThoughtsExpanded(messageId);
}
function isThoughtsExpanded(messageId: string): boolean {
  if (expandedThoughts.value[messageId] !== undefined) {
    return expandedThoughts.value[messageId];
  }
  return true;
}

function send(value = draft.value) {
  let v = value.trim();
  if (!v && props.pendingReplaceDish) {
    v = `换掉${props.pendingReplaceDish.dish_name}，推荐一道别的菜`;
  }
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
        <p>先在上方选择参与成员，再描述口味、忌口或用餐场景。</p>
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
            <!-- 现代思维链可折叠卡片 (Thinking Process) -->
            <div
              v-if="m.thoughts && m.thoughts.length"
              class="thought-box"
              :class="{ 'thought-collapsed': !isThoughtsExpanded(m.id) }"
              data-testid="thought-process-container"
            >
              <button
                type="button"
                class="thought-header-btn"
                @click="toggleThoughts(m.id)"
              >
                <div class="thought-summary-title">
                  <BrainCircuit :size="13" class="thought-icon" />
                  <span v-if="m.status === 'sending'" class="thought-running-text">
                    <LoaderCircle class="spin" :size="12" /> 执行规划中 ({{ m.thoughts.length }} 阶段)...
                  </span>
                  <span v-else-if="m.status === 'cancelled'" class="thought-done-text thought-cancelled-text">
                    用户已停止执行 ({{ m.thoughts.length }} 阶段)
                  </span>
                  <span v-else-if="m.status === 'error'" class="thought-done-text thought-error-text">
                    执行未完成已终止 ({{ m.thoughts.length }} 阶段)
                  </span>
                  <span v-else-if="isMessageCommitted(m)" class="thought-done-text">
                    执行完成已提交 ({{ m.thoughts.length }} 阶段)
                  </span>
                  <span v-else class="thought-done-text">
                    执行记录 ({{ m.thoughts.length }} 阶段)
                  </span>
                </div>
                <ChevronDown
                  :size="14"
                  class="thought-arrow"
                  :class="{ 'rotate-180': isThoughtsExpanded(m.id) }"
                />
              </button>

              <ol v-if="isThoughtsExpanded(m.id)" class="thought-timeline">
                <li
                  v-for="(t, idx) in m.thoughts"
                  :key="t.invocation_id ? `${t.execution_generation}:${t.invocation_id}:${t.node_id}` : t.node_id || idx"
                  class="thought-step"
                  :class="t.status"
                >
                  <div class="step-dot">
                    <CheckCircle2 v-if="t.status === 'done'" :size="12" />
                    <LoaderCircle v-else-if="t.status === 'running'" class="spin" :size="12" />
                    <span v-else class="dot-inner"></span>
                  </div>
                  <div class="step-content">
                    <div class="step-title-line">
                      <span class="step-title">{{ t.title }}</span>
                      <span v-if="t.tool_name" class="step-tool-badge">{{ t.tool_name }}</span>
                    </div>
                    <p v-if="t.summary" class="step-summary">{{ t.summary }}</p>
                  </div>
                </li>
              </ol>
            </div>

            <p v-else-if="m.status === 'sending' && !m.content && !m.clarification" class="thinking">
              <LoaderCircle class="spin" :size="14" /> 正在分析…
            </p>
            <p v-if="m.content && (!m.clarification || m.content !== '待澄清')" class="answer-text">{{ m.content }}</p>
            <p v-if="isMessageCommitted(m)" class="answer-badge">
              <CheckCircle2 :size="13" /> 菜单已生成
            </p>
            <p v-else-if="isMessageAnswered(m)" class="answer-badge pending">
              <LoaderCircle class="spin" :size="13" /> 待最终确认
            </p>
            <p v-else-if="m.status === 'cancelled'" class="answer-badge cancelled">
              已停止生成
            </p>
            <p v-if="m.status === 'error'" class="error-text">{{ m.content }}</p>

            <!-- 消息绑定的内联澄清卡片 (R06) -->
            <div
              v-if="m.clarification && m.clarification.options && m.clarification.options.length"
              class="message-clarification-card"
              :class="{
                'is-answered': !!m.selectedOptionId,
                'is-active': isClarificationActive(m),
                'is-stale': !isClarificationActive(m) && !m.selectedOptionId,
              }"
              role="region"
              aria-label="澄清选项"
            >
              <div class="clarification-card-header">
                <span
                  class="clarification-status-tag"
                  :class="{
                    'status-selected': !!m.selectedOptionId,
                    'status-pending': isClarificationActive(m) && !m.selectedOptionId,
                    'status-stale': !isClarificationActive(m) && !m.selectedOptionId,
                  }"
                >
                  {{ clarificationStatusText(m) }}
                </span>
                <span class="clarification-card-question">{{ m.clarification.question_text }}</span>
              </div>
              <div class="clarification-card-options">
                <button
                  v-for="opt in m.clarification.options"
                  :key="opt.option_id"
                  type="button"
                  class="clarification-option-btn"
                  :class="{
                    'selected': m.selectedOptionId === opt.option_id,
                    'disabled': !canClickClarificationOption(m),
                  }"
                  :disabled="!canClickClarificationOption(m)"
                  @click="emit('selectOption', opt.option_id, m.id)"
                >
                  <span class="option-num">{{ opt.option_id }}.</span>
                  <span class="option-text">{{ opt.text }}</span>
                  <span v-if="m.selectedOptionId === opt.option_id" class="option-badge-selected">
                    <CheckCircle2 :size="12" /> 已选
                  </span>
                </button>
              </div>
            </div>
          </template>
        </div>
      </article>

      <section
        v-if="currentMenu"
        class="committed-menu"
        data-testid="committed-menu"
        aria-label="已提交菜单"
      >
        <div class="committed-menu-header">
          <div class="committed-menu-title">
            <CheckCircle2 :size="16" />
            <span>健康推荐菜单</span>
            <span class="menu-count-badge">{{ currentMenu.items.length }} 道菜品</span>
          </div>
          <div class="menu-meta-badge">已完成健康合规审查</div>
        </div>
        <ol class="menu-card-grid">
          <li
            v-for="(item, idx) in currentMenu.items"
            :key="item.recipe_id"
            class="menu-dish-card"
          >
            <div class="dish-index">{{ idx + 1 }}</div>
            <div class="dish-body">
              <span class="dish-name">{{ item.name }}</span>
              <span class="dish-tag">#{{ item.recipe_id }}</span>
            </div>
            <button
              type="button"
              class="dish-replace-btn"
              :class="{ 'is-active': props.pendingReplaceDish?.target_recipe_id === item.recipe_id }"
              :disabled="props.isStreaming"
              :title="props.pendingReplaceDish?.target_recipe_id === item.recipe_id ? '当前已锁定该菜品进行单菜替换' : '换这道（锁定菜品与菜单版本）'"
              data-testid="dish-replace-button"
              @click="onReplaceDish(item)"
            >
              {{ props.pendingReplaceDish?.target_recipe_id === item.recipe_id ? '替换中' : '换这道' }}
            </button>
          </li>
        </ol>
      </section>

      <!-- 消息流内联澄清选项 (独立兜底组件：仅当未绑定到具体消息时展示) -->
      <div
        v-if="!hasMessageBoundClarification && props.activeClarification && props.activeClarification.options && props.activeClarification.options.length"
        class="clarification-panel inline-clarification"
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
            :disabled="props.isStreaming || props.isCancelling"
            @click="emit('selectOption', opt.option_id)"
          >
            <span class="option-num">{{ opt.option_id }}.</span> {{ opt.text }}
          </button>
        </div>
      </div>
      <div v-else-if="!hasMessageBoundClarification && needsClarification" class="clarification-bar inline-clarification" role="status">
        <span class="clarification-label">待确认</span>
        <span class="clarification-text">{{ props.clarification }}</span>
      </div>
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

    <div v-if="status && !isStreaming" class="terminal-bar" :class="`term-${status}`" role="status">
      {{ statusLabel }}
    </div>

    <!-- 结构化单菜替换锁定提示条 (R07) -->
    <div
      v-if="props.pendingReplaceDish"
      class="replace-lock-banner"
      data-testid="replace-lock-banner"
    >
      <div class="replace-lock-info">
        <Sparkles :size="13" class="replace-lock-icon" />
        <span class="replace-lock-label">正在替换</span>
        <span class="replace-lock-name">{{ props.pendingReplaceDish.dish_name }}</span>
        <span class="replace-lock-hash">#{{ props.pendingReplaceDish.target_recipe_id }}（版本已锁定）</span>
      </div>
      <button
        type="button"
        class="replace-lock-cancel-btn"
        title="取消替换"
        data-testid="cancel-replace-btn"
        @click="emit('cancelReplaceDish')"
      >
        ✕ 取消替换
      </button>
    </div>

    <div class="composer">
      <textarea
        v-model="draft"
        rows="1"
        maxlength="1000"
        :disabled="isStreaming"
        :placeholder="props.pendingReplaceDish ? `可输入对替换「${props.pendingReplaceDish.dish_name}」的偏好（如：清淡、换蔬菜），直接回车发送…` : '描述口味、食材、餐次或临时健康情况…'"
        @keydown="onKeydown"
      />
      <button
        v-if="isStreaming"
        class="send-button stop-button"
        type="button"
        :disabled="props.isCancelling"
        aria-label="停止生成"
        :title="props.isCancelling ? '正在停止…' : '停止生成'"
        data-testid="stop-generating-button"
        @click="emit('cancel')"
      >
        <LoaderCircle v-if="props.isCancelling" class="spin" :size="16" />
        <Square v-else :size="16" fill="currentColor" />
      </button>
      <button
        v-else
        class="send-button"
        type="button"
        :disabled="!canSend || (!draft.trim() && !props.pendingReplaceDish)"
        aria-label="发送"
        @click="send()"
      ><ArrowUp :size="19" /></button>
    </div>
  </main>
</template>
