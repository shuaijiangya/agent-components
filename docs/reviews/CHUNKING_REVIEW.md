# 分块功能实现检查记录

本轮按此前 CHUNKING.md 的全部“未实现／部分实现／未封装”条目补齐组件，并扩展来源、流式和资源检查。Python 3.12.14、Chonkie 1.7.0、Docling 2.150.1；依赖未新增未锁定版本。

每行对应一个具体行为，分别给出函数和测试；代码入口和参数说明见 [CHUNKING.md](../CHUNKING.md)，完整代码文本见 [代码 Markdown](../code/CHUNKING_CODE_REVIEW.md)。以下“通过”仅覆盖所列测试条件。

## 功能—逻辑—代码—测试

| 编号 | 行为及验收条件 | 实现函数／模块 | 对应测试 | 结果 |
| --- | --- | --- | --- | --- |
| CH-01 | 六种策略、严格整数、有效重叠和非空唯一分隔符 | chunking.ChunkPolicy.validate_policy | test_policy_rejects_invalid_values_at_construction；test_extended_policy_rejects_ambiguous_or_unsafe_values | 通过 |
| CH-02 | model_copy 绕过校验后，空输入也拒绝非法策略参数 | chunking._validated_policy | test_bypassed_policy_is_revalidated_even_for_empty_inputs | 通过 |
| CH-03 | 字符原文切片、尾块、最大重叠仍前进 | chunking.Chunker._plain_spans | test_character_overlap_offsets_and_ids_are_deterministic；test_maximum_character_overlap_progresses_and_preserves_final_boundary | 通过 |
| CH-04 | CRLF、空白、Emoji、组合字符不被丢弃 | chunking.Chunker.chunk | test_character_preserves_every_python_character；test_real_recursive_preserves_edge_cases_and_size_limit | 通过 |
| CH-05 | 自定义分隔符为字面量，归前／后均保留，不误执行正则 | chunking_backends.literal_spans | test_literal_delimiters_keep_repeated_unicode_and_regex_characters | 通过 |
| CH-06 | 原生递归规则可配置，预留预算后补重叠 | chunking._recursive_spans／Chunker._plain_spans | test_chonkie_extended_rules_and_overlap；test_real_recursive_prefers_paragraph_sentence_and_whitespace_boundaries | 真实 Chonkie 通过 |
| CH-07 | 句子策略保留句末，单片句数可控，超长句继续拆分 | chunking_backends.sentence_spans | test_sentence_strategy_keeps_single_sentences_and_splits_overlong_sentence | 真实 Chonkie 通过 |
| CH-08 | Token 预算使用实际编码；中文／Emoji／空白无损 | chunking_backends.token_spans／token_count | test_real_token_budget_preserves_chinese_emoji_and_all_whitespace | 通过 |
| CH-09 | 真实 BPE 编码器在预算内覆盖原文，输出不依赖解码半个 Token | chunking.Chunker＋TokenEncoder | test_offline_native_bpe_tokenizer_with_actual_token_budget | 原生 tokenizers BPE 通过 |
| CH-10 | Token 必须配置能力及版本身份，小预算无法容纳字符时报错 | chunking.Chunker.__init__／configuration_fingerprint | test_token_budget_and_backend_identity_are_enforced | 通过 |
| CH-11 | 原生语义算法使用注入向量并识别测试主题边界 | chunking_backends.semantic_spans | test_semantic_uses_native_chonkie_and_bounded_injected_embeddings | 真实 Chonkie 通过；非业务准确率评测 |
| CH-12 | 向量请求有批次上限，错误数量／维度／NaN／零向量均拒绝 | chunking_backends.semantic_spans.Bridge | test_semantic_uses_native_chonkie_and_bounded_injected_embeddings；test_semantic_rejects_invalid_embedding_outputs | 通过 |
| CH-13 | 表格只切完整数据行，后续片单独保存表头及来源 | tables.split_table／contracts.TableSlice | test_table_is_split_by_complete_rows_with_independent_repeated_header | 通过 |
| CH-14 | 单列表格和真实 Token 预算下的表格也保持整行 | tables.markdown_table_ranges／split_table | test_single_column_table_and_token_table_budget | 通过 |
| CH-15 | 不可容纳单行默认失败；显式 allow 标识超限；关闭保持时按普通策略切 | tables.split_table／chunking.Chunker.chunk | test_table_overflow_is_explicit_and_disabling_preservation_uses_normal_budget | 通过 |
| CH-16 | 代码围栏中的表格不被识别，较短／异种围栏不能错误关闭 | tables.markdown_table_ranges | test_fenced_markdown_table_is_plain_code；test_mismatched_or_shorter_fence_does_not_expose_code_as_table | 通过 |
| CH-17 | 重复原文按序列化位置区分，原生来源不通过首次 find 猜测 | parsing.DoclingParser.from_native | test_native_docling_repeated_text_heading_levels_and_page_provenance | 原生 DoclingDocument 通过 |
| CH-18 | Markdown／HTML／DOCX 得到真实标题和表格，并保留数字精度 | parsing.DoclingParser.parse／from_native | test_real_docling_files_produce_sections_mapped_tables_and_preserve_numeric_text；test_real_docling_docx_structures_and_mapped_table | 真实文件通过 |
| CH-19 | DOCX 整章 Group 不得掩盖内部表格 | parsing.DoclingParser.from_native | test_real_docling_docx_structures_and_mapped_table | 已修复并通过 |
| CH-20 | SourceMap 可查相交原生元素、页码、bbox；越界映射被拒绝 | sources.SourceMapper.resolve／contracts.Document.validate_structure | test_structure_and_source_map_validation_and_intersection；test_native_docling_repeated_text_heading_levels_and_page_provenance | 通过；元素级精度 |
| CH-21 | 无 SourceMap 时仅给文本来源并提示，不伪造页码 | chunking._source_warnings／_chunk | test_upstream_file_sources_are_not_misrepresented_as_chunk_sources | 通过 |
| CH-22 | 章节按原生 level，保留序言、最大深度和祖先 path | sections.SectionBuilder.build／ParentChildBuilder.build_sections | test_sections_use_native_levels_preserve_preamble_and_hierarchical_path | 通过 |
| CH-23 | 子块不越父范围，局部转全局一次，两种序号独立 | chunking.ParentChildBuilder.build | test_parent_child_ranges_cannot_cross_parents；test_parent_offsets_ordinals_and_partial_coverage_are_explicit；test_real_recursive_parent_children_use_global_coordinates | 通过 |
| CH-24 | 全部父范围在任何子块工作前完整验证 | chunking._validated_ranges／ParentChildBuilder.build | test_parent_ranges_are_validated_before_any_child_work | 通过 |
| CH-25 | 父范围切穿受保护表格时先报错，不先执行其他父块 | chunking.ParentChildBuilder.build | test_parent_table_protection_is_preflight_and_warning_ids_are_global | 通过 |
| CH-26 | 父子模式的 warning 引用最终全局块 ID | chunking.ParentChildBuilder.build | test_parent_table_protection_is_preflight_and_warning_ids_are_global | 通过 |
| CH-27 | 内容／版本／文档／策略／父布局／后端身份参与块身份 | chunking._chunk／_fingerprint | test_changed_content_does_not_reuse_ids_when_revision_was_not_updated；test_ids_bind_document_policy_and_parent_layout；test_token_budget_and_backend_identity_are_enforced | 通过 |
| CH-28 | 空输入、后端错位／缺失／重叠／超预算有明确行为 | chunking.Chunker.chunk／_recursive_spans | test_empty_document_has_explicit_empty_coverage；test_recursive_rejects_broken_backend_output | 通过 |
| CH-29 | 核心字符分块与空递归不导入 Chonkie，缺依赖错误明确 | chunking.Chunker.chunk | test_character_and_empty_recursive_do_not_import_chonkie；test_recursive_missing_dependency_has_actionable_error | 通过 |
| CH-30 | 子命中补父章节，append／replace 均合并重复正文 | context.ContextExpander.expand／_assemble | test_parent_retrieval_supplements_full_chapter_and_removes_duplicate_text | 通过 |
| CH-31 | 邻块按文档、版本和父范围隔离 | context.ContextExpander.expand | test_expansion_stays_within_document_version_and_parent_then_merges_overlap | 通过 |
| CH-32 | 缺失可见邻块不被跨越，命中与父关系必须匹配可见语料 | context.ContextExpander.expand | test_neighbor_expansion_stops_at_invisible_gap_and_budget_is_reported；test_expansion_rejects_out_of_corpus_hits_missing_parents_and_conflicting_overlap | 通过 |
| CH-33 | 重叠内容按原文去重，冲突正文拒绝 | context.ContextExpander._assemble | test_expansion_stays_within_document_version_and_parent_then_merges_overlap；test_expansion_rejects_out_of_corpus_hits_missing_parents_and_conflicting_overlap | 通过 |
| CH-34 | 上下文预算不裁正文，遗漏身份明确 | context.ContextExpander.expand | test_neighbor_expansion_stops_at_invisible_gap_and_budget_is_reported | 通过 |
| CH-35 | MMR 的相关性权重改变重复／多样证据的选择，同分稳定 | selection.mmr_select | test_mmr_trades_relevance_for_diverse_evidence_and_is_stable | 通过 |
| CH-36 | 规范化精确重复和中文近重复有 duplicate_of／相似度／原因 | selection.deduplicate | test_exact_and_chinese_near_duplicate_decisions_are_traceable | 通过 |
| CH-37 | MMR 拒绝空／零／NaN／Inf／错误维度向量并限制候选数 | selection._unit／mmr_select | test_mmr_rejects_invalid_query_or_dimensions；test_mmr_limits_candidate_work_and_requires_vectors | 通过 |
| CH-38 | 字符流与 UTF-8 BOM 文件保持 CRLF、全局偏移、ID 不依赖传输切口 | streaming.StreamingChunker.iter_text／iter_file | test_stream_preserves_unicode_crlf_global_offsets_and_transport_independent_identity | 通过 |
| CH-39 | 六种策略都可窗口化处理并验证真实位置与预算 | streaming.StreamingChunker.iter_text | test_every_strategy_streams_with_valid_global_positions；test_stream_preserves_unicode_crlf_global_offsets_and_transport_independent_identity | 通过 |
| CH-40 | 长表格跨窗口按完整行读取并保持原始表头位置 | streaming.StreamingChunker._segments | test_streaming_long_table_preserves_every_row_across_analysis_windows | 通过 |
| CH-41 | 大输入／过多块／非字符串／超长行明确失败；巨大 size 不让表格缓冲无限增长 | streaming.StreamingChunker | test_streaming_rejects_resource_and_unbounded_row_limits；test_stream_fence_tracking_and_window_limit_for_oversized_table_budget | 通过 |
| CH-42 | 共享组件八线程输出确定 | streaming.StreamingChunker | test_streaming_shared_component_is_deterministic_under_threads | 通过 |
| CH-43 | 流式字符重叠在分析窗口接缝仍保持 | streaming.StreamingChunker.iter_text | test_character_overlap_is_preserved_at_streaming_window_seams | 通过 |
| CH-44 | PDF 页范围覆盖所有页且分段身份不混用 | parsing.DoclingParser.iter_pdf | test_pdf_batch_ranges_use_real_pdf_page_count_and_keep_distinct_segment_ids | 调度检查通过，非权重推理验收 |
| CH-45 | 约 71 MB UTF-8 大文本完整覆盖，有界 Python 内存；八线程 16 请求一致 | scripts/benchmark_chunking.py | consume／main，原始记录 docs/benchmarks/chunking-local.json | 本地实际执行通过 |

## 测试文件定位

- [原分块回归](../../tests/test_chunking.py)
- [扩展策略、章节、表格、SourceMap](../../tests/test_chunking_extended.py)
- [流式、资源上限与线程](../../tests/test_chunking_streaming.py)
- [上下文、MMR 与去重](../../tests/test_context_selection.py)
- [真实 Docling 结构、DOCX 和 PDF 分页调度](../../tests/test_docling_structure.py)
- [解析基础回归](../../tests/test_parsing.py)

## 关键实现决定

1. Token 采用原文字符边界和实际编码计量，避免原生 Token 分组解码在中文／Emoji 边界制造替换字符。
2. 语义实际调用 Chonkie SemanticChunker；使用原生默认平滑参数，向量提供方明确注入，测试能检查主题边界而非仅检查“返回非空”。
3. Docling 按原生 DocItem 序列化并累计偏移；DOCX 的整章 Group 是已覆盖的实际回归。
4. 表格正文与重复表头分离。预算包含表头；不可拆单行超限必须明确报错或由使用方显式允许。
5. 父子范围、相邻上下文和 MMR／去重是分别可调用的组件，不把检索后的能力塞进分块策略，不决定业务执行顺序。
6. 源码中各入口有中文多行说明，关键不变量有中文行内注释；Ruff D 规则持续检查。

## 验收边界

- 文件位置为 Docling 元素／页码／bbox 精度，不是原 PDF 字符到字形的逐字对应。
- 语义提供方的实际模型质量、延迟和费用需按业务数据评测；本次测试不调用付费模型或下载权重。
- PDF 分页入口已实现；调度用真实 PDF 页数和转换器替身验证。实际 Docling PDF 推理需准备离线权重后验收。
- PDF 分批不会自动恢复跨批章节或跨页表格；Office 使用原生单文件转换。规范文本可用流式组件分块。
- 资源数据仅证明本机测试条件下的覆盖和资源开销，不作为任意模型或硬件的 SLA。
- 完整知识库发布、权限决策、索引删除和业务执行不在分块组件职责内。

本轮改变解析结构和块身份，旧产物必须重建；完整执行结果及复查命令见 [VALIDATION.md](../VALIDATION.md)。
