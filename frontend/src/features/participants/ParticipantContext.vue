<script setup lang="ts">
import { computed, ref } from "vue";
import { ChevronDown, UserRound, X } from "@lucide/vue";
import type { UserProfile } from "@/types";

const props = defineProps<{
  profiles: UserProfile[];
  selectedIds: number[];
  loading: boolean;
  disabled: boolean;
}>();
const emit = defineEmits<{
  bind: [id: number];
  remove: [id: number];
}>();

const open = ref(false);
const selected = computed(() =>
  props.profiles.filter((p) => props.selectedIds.includes(p.id)),
);
const available = computed(() =>
  props.profiles.filter((p) => !props.selectedIds.includes(p.id)),
);

function profileNote(p: UserProfile): string {
  const parts: string[] = [];
  if (p.special_group?.length) parts.push(p.special_group.join("、"));
  if (p.allergies?.length) parts.push(`过敏:${p.allergies.join("、")}`);
  return parts.join(" | ") || (p.age ? `${p.age}岁` : "无特殊");
}
</script>

<template>
  <div class="participant-context">
    <div class="chip-list">
      <span v-for="p in selected" :key="p.id" class="chip">
        <UserRound :size="13" />
        <span>{{ p.display_name }}</span>
        <i class="chip-note">{{ profileNote(p) }}</i>
        <button type="button" class="chip-x" :disabled="disabled" @click="emit('remove', p.id)">
          <X :size="12" />
        </button>
      </span>
    </div>

    <div class="picker" :class="{ open }">
      <button
        type="button"
        class="picker-trigger"
        :disabled="disabled"
        @click="open = !open"
      >
        <UserRound :size="15" />
        <span>选择健康档案</span>
        <ChevronDown :size="14" />
      </button>
      <div v-if="open" class="picker-menu">
        <p v-if="loading" class="picker-hint">加载中…</p>
        <p v-else-if="!available.length" class="picker-hint">已全部选中</p>
        <button
          v-for="p in available"
          :key="p.id"
          type="button"
          class="picker-item"
          @click="emit('bind', p.id)"
        >
          <span class="pi-name">{{ p.display_name }}</span>
          <span class="pi-note">{{ profileNote(p) }}</span>
        </button>
      </div>
    </div>
  </div>
</template>
