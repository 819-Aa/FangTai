<script setup lang="ts">
import { computed, ref } from "vue";
import { Check, ChevronDown, UserRound, X } from "@lucide/vue";
import type { AnonymousParticipant } from "@/types";

const props = defineProps<{
  slots: AnonymousParticipant[];
  selectedRefs: string[];
  disabled: boolean;
}>();
const emit = defineEmits<{
  toggle: [ref: string];
}>();

const open = ref(false);
const query = ref("");
const selectedSlots = computed(() =>
  props.slots.filter((slot) => props.selectedRefs.includes(slot.participant_ref)),
);
const filteredSlots = computed(() =>
  props.slots.filter((slot) =>
    `${slot.label} ${slot.participant_ref}`.toLowerCase().includes(query.value.trim().toLowerCase()),
  ),
);

function togglePicker() {
  if (props.disabled) return;
  open.value = !open.value;
  if (!open.value) query.value = "";
}
</script>

<template>
  <div class="participant-context">
    <div class="chip-list">
      <span v-for="slot in selectedSlots" :key="slot.participant_ref" class="chip selected">
        <UserRound :size="13" />
        <span>{{ slot.label }}</span>
        <button
          type="button"
          class="chip-x"
          :aria-label="`取消选择${slot.label}`"
          :disabled="disabled"
          @click="emit('toggle', slot.participant_ref)"
        ><X :size="12" /></button>
      </span>
    </div>

    <div class="picker">
      <button
        type="button"
        class="picker-trigger"
        :aria-expanded="open"
        aria-controls="participant-picker-menu"
        :disabled="disabled"
        @click="togglePicker"
      >
        <UserRound :size="15" />
        <span>选择参与成员{{ selectedRefs.length ? `（${selectedRefs.length}）` : '' }}</span>
        <ChevronDown :size="14" />
      </button>
      <div v-if="open && !disabled" id="participant-picker-menu" class="picker-menu">
        <input v-model="query" class="picker-search" type="search" placeholder="搜索成员编号" aria-label="搜索成员" />
        <p v-if="!filteredSlots.length" class="picker-hint">没有匹配的成员</p>
        <button
          v-for="slot in filteredSlots"
          :key="slot.participant_ref"
          type="button"
          class="picker-item"
          :aria-pressed="selectedRefs.includes(slot.participant_ref)"
          @click="emit('toggle', slot.participant_ref)"
        >
          <span class="pi-name">{{ slot.label }}</span>
          <Check v-if="selectedRefs.includes(slot.participant_ref)" :size="15" />
        </button>
      </div>
    </div>
    <span v-if="!selectedRefs.length" class="participant-hint" role="status">请先选择参与成员</span>
    <span v-else-if="disabled" class="participant-hint">更换成员请新建对话</span>
  </div>
</template>
