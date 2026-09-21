// 看板 API 数据类型定义

export interface SummaryData {
  equipment: number
  sample_total: number
  month_count: number
  year: number
}

export interface MonthlyData {
  labels: string[]
  values: number[]
  year: number
}

export interface ProductData {
  labels: string[]
  values: number[]
  year: number
}

export interface CompletionData {
  labels: string[]
  series: Record<string, number[]>
  total: number
  year: number
}

export interface ItemCountData {
  labels: string[]
  series: Record<string, number[]>
  categories: { labels: string[]; values: number[] }
  total: number
  year: number
}

export interface DetectRateSeries {
  total: number
  pos: number
}

export interface DetectRateData {
  labels: string[]
  series: Record<string, DetectRateSeries[]>
  year: number
}

export interface TopItem {
  name: string
  cnt: number
}

export interface CategoryTop {
  projects: TopItem[]
  samples: TopItem[]
}

export interface TopData {
  categories: Record<string, CategoryTop>
  year: number
}

export interface RegionData {
  labels: string[]
  values: number[]
  totals: number[]
  fails: number[]
  year: number
}

export type ApiResult<T> = { success: true; data: T } | { success: false; error: string }
