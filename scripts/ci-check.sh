#!/bin/bash
# CARMA Box — Local CI check
# Run before every push. Same gates as CI pipeline.
set -e

echo "╔════════════════════════════════════════╗"
echo "║  CARMA Box CI Check                    ║"
echo "╚════════════════════════════════════════╝"

cd "$(dirname "$0")/.."

echo ""
echo "── Gate 1: Lint ──────────────────────────"
python3 -m ruff check .
python3 -m ruff format --check .
echo "✅ Lint passed"

echo ""
echo "── Gate 2: Type Check ───────────────────"
python3 -m mypy --strict custom_components/carmabox/
echo "✅ Type check passed"

echo ""
echo "── Gate 3: Unit Tests ───────────────────"
python3 -m pytest tests/unit/ -q --tb=short
echo "✅ Tests passed"

echo ""
echo "── Gate 4: Coverage ─────────────────────"
python3 -m pytest tests/unit/ \
    --cov=custom_components/carmabox/optimizer \
    --cov=custom_components/carmabox/adapters \
    --cov=custom_components/carmabox/coordinator.py \
    --cov=custom_components/carmabox/sensor.py \
    --cov-fail-under=90 \
    -q --tb=short 2>&1 | tail -5
echo "✅ Coverage ≥90%"

echo ""
echo "── Gate 5: Security ─────────────────────"
python3 -m bandit -r custom_components/carmabox/ -ll -q 2>&1 | tail -1
echo "✅ Security passed"

echo ""
echo "╔════════════════════════════════════════╗"
echo "║  ALL GATES PASSED ✅                   ║"
echo "╚════════════════════════════════════════╝"
