import os
import numpy as np
import pandas as pd
from xgboost import XGBRanker
from sklearn.metrics import ndcg_score
from features import FEATURE_COLUMNS, prepare_features

def precision_at_k(y_true, y_score, k):
    if len(y_true) == 0: return 0.0
    sorted_indices = np.argsort(y_score)[::-1]
    top_k_indices = sorted_indices[:k]
    return y_true.iloc[top_k_indices].sum() / k

def evaluate_ranking(df_test, score_col):
    snapshots_with_deals = df_test.groupby('snapshot_date')['target_acquired_12m'].sum()
    valid_snapshots = snapshots_with_deals[snapshots_with_deals > 0].index
    df_eval = df_test[df_test['snapshot_date'].isin(valid_snapshots)].copy()

    if df_eval.empty:
        return {'ndcg_10': 0.0, 'ndcg_25': 0.0, 'p_10': 0.0, 'p_25': 0.0, 'snapshots': 0}

    ndcg_10_list, ndcg_25_list = [], []
    p_10_list, p_25_list = [], []

    for _, group in df_eval.groupby('snapshot_date'):
        y_true = group['target_acquired_12m']
        y_score = group[score_col]

        ndcg_10_list.append(ndcg_score([y_true.values], [y_score.values], k=10))
        ndcg_25_list.append(ndcg_score([y_true.values], [y_score.values], k=25))
        p_10_list.append(precision_at_k(y_true, y_score, 10))
        p_25_list.append(precision_at_k(y_true, y_score, 25))

    return {
        'ndcg_10': np.mean(ndcg_10_list),
        'ndcg_25': np.mean(ndcg_25_list),
        'p_10': np.mean(p_10_list),
        'p_25': np.mean(p_25_list),
        'snapshots': len(valid_snapshots)
    }

def run_experiment():
    matrix_path = "historical_ml_matrix.csv"
    if not os.path.exists(matrix_path):
        print("❌ Error: 'historical_ml_matrix.csv' missing.")
        return

    df = pd.read_csv(matrix_path).dropna(subset=['target_acquired_12m'])
    df = prepare_features(df)
    df['snapshot_date'] = pd.to_datetime(df['snapshot_date'])
    df = df.sort_values(['snapshot_date', 'cik'])

    train_mask = df['snapshot_date'].dt.year <= 2022
    test_mask  = df['snapshot_date'].dt.year >= 2023

    df_train = df[train_mask].copy()
    df_test  = df[test_mask].copy()

    X_test = df_test[FEATURE_COLUMNS]

    # --- Variant A: Full Universe (Natively Learns Market Cap & Runway) ---
    print("🔬 Training Variant A (Full Universe - Feature Learning)...")
    groups_a = df_train.groupby('snapshot_date').size().values
    ranker_a = XGBRanker(
        n_estimators=300, learning_rate=0.015, max_depth=4,
        subsample=0.8, colsample_bytree=0.8, reg_alpha=2.0, reg_lambda=5.0,
        random_state=42, objective='rank:pairwise', eval_metric='ndcg'
    )
    ranker_a.fit(df_train[FEATURE_COLUMNS], df_train['target_acquired_12m'], group=groups_a)
    df_test['score_variant_a'] = ranker_a.predict(X_test)

    # --- Variant B: Hard Filtered Universe ---
    print("🔬 Training Variant B (Hard Filtered Universe)...")
    filter_mask = (df_train['runway_months'] <= 36) | (df_train['runway_months'].isna())
    if 'market_cap_usd' in df_train.columns:
        filter_mask = filter_mask & ((df_train['market_cap_usd'] >= 30_000_000) | (df_train['market_cap_usd'].isna()))

    df_train_b = df_train[filter_mask].copy()
    groups_b = df_train_b.groupby('snapshot_date').size().values

    ranker_b = XGBRanker(
        n_estimators=300, learning_rate=0.015, max_depth=4,
        subsample=0.8, colsample_bytree=0.8, reg_alpha=2.0, reg_lambda=5.0,
        random_state=42, objective='rank:pairwise', eval_metric='ndcg'
    )
    ranker_b.fit(df_train_b[FEATURE_COLUMNS], df_train_b['target_acquired_12m'], group=groups_b)
    df_test['score_variant_b'] = ranker_b.predict(X_test)

    # --- Evaluate Both Variants ---
    metrics_a = evaluate_ranking(df_test, 'score_variant_a')
    metrics_b = evaluate_ranking(df_test, 'score_variant_b')

    print("\n==================================================")
    print("A/B EXPERIMENT RESULTS (8 Out-of-Sample Test Quarters)")
    print("==================================================")
    print(f"{'Metric':<20} | {'Variant A (Full)':<18} | {'Variant B (Filtered)':<18}")
    print("-" * 62)
    print(f"{'NDCG @ 10':<20} | {metrics_a['ndcg_10']:<18.4f} | {metrics_b['ndcg_10']:<18.4f}")
    print(f"{'NDCG @ 25':<20} | {metrics_a['ndcg_25']:<18.4f} | {metrics_b['ndcg_25']:<18.4f}")
    print(f"{'Precision @ 10':<20} | {metrics_a['p_10']:<18.4f} | {metrics_b['p_10']:<18.4f}")
    print(f"{'Precision @ 25':<20} | {metrics_a['p_25']:<18.4f} | {metrics_b['p_25']:<18.4f}")
    print(f"{'Evaluated Snapshots':<20} | {metrics_a['snapshots']:<18} | {metrics_b['snapshots']:<18}")
    print("==================================================")

if __name__ == "__main__":
    run_experiment()
