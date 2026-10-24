# 누끼 자동 처리·등록 시스템 (Nukki Auto Background-Removal Service)

플랫폼에 올라오는 **사진의 배경을 자동으로 제거(누끼)하고 DB에 등록**하는 완성형 서비스입니다.
GitHub의 최고 성능 오픈소스 누끼 엔진(`rembg` + MIT 라이선스 모델)을 **실사용성 검증**(→ [BENCHMARK.md](BENCHMARK.md))
후, 상용 가능한 기본값으로 묶어 FastAPI + Docker로 패키징했습니다.

```
업로드(API/UI/배치)  ─►  누끼 엔진(rembg)  ─►  저장(로컬/S3)  ─►  DB 등록(상태·URL·품질지표)
```

사진 한 장을 올리면 자동으로:
1. 배경 제거 → **투명 PNG**
2. 흰 배경 합성 → **마켓플레이스용 JPG** (쿠팡/아마존 등 흰 배경 요구 대응)
3. 썸네일 생성
4. 원본·결과 URL과 품질지표를 **DB에 한 행으로 등록**

---

## 왜 이 구성인가 (검증 요약)

| 환경 | 기본 모델 | 장당 | 품질 | 라이선스 |
|---|---|---|---|---|
| **CPU (기본값)** | `isnet-general-use` | ~1.2s | 상용 수준 | **MIT ✅ 상업 가능** |
| GPU / 최고 품질 | `birefnet-general` | 1~2s(GPU) | 최상 | **MIT ✅** |

> CPU에서 `birefnet-general`은 장당 ~28초·RAM 9GB로 비현실적이라, 품질이 사실상 동급이면서
> 10배 빠른 `isnet-general-use`를 기본값으로 선택했습니다. 전체 수치·비교 이미지는 [BENCHMARK.md](BENCHMARK.md).

---

## 빠른 시작

### Docker (권장)

```bash
docker compose up --build
# → http://localhost:8000      (테스트 UI)
# → http://localhost:8000/docs (OpenAPI 문서)
```

모델은 빌드 시 이미지에 구워져 첫 요청이 바로 빠릅니다. GPU 호스트라면:

```bash
REMBG_MODEL=birefnet-general docker compose up --build
```

### 로컬 (venv)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

---

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| `POST` | `/api/v1/images` | 사진 업로드 → 자동 누끼 + 등록. `?background=true`면 비동기(202) |
| `GET` | `/api/v1/images/{id}` | 등록 레코드 조회 |
| `GET` | `/api/v1/images` | 목록 (`status`, `external_id`, `limit`, `offset` 필터) |
| `DELETE` | `/api/v1/images/{id}` | 레코드 삭제 |
| `GET` | `/health` | 헬스체크 + 로드된 모델 |
| `GET` | `/` | 드래그&드롭 테스트 UI |

### 업로드 예시

```bash
curl -F "file=@product.jpg" -F "external_id=SKU-12345" \
     http://localhost:8000/api/v1/images
```

```json
{
  "id": "14b03b659fba450ea4ee50e179f1fafe",
  "external_id": "SKU-12345",
  "status": "done",
  "model": "isnet-general-use",
  "original_url": "http://localhost:8000/files/2026/10/.../original.jpg",
  "cutout_url":   "http://localhost:8000/files/2026/10/.../cutout.png",
  "flat_url":     "http://localhost:8000/files/2026/10/.../flat.jpg",
  "thumbnail_url":"http://localhost:8000/files/2026/10/.../thumb.png",
  "width": 1000, "height": 726,
  "processing_ms": 931,
  "foreground_ratio": 0.178,
  "needs_review": false
}
```

- `external_id`로 플랫폼의 상품/엔티티와 결과를 연결합니다.
- `foreground_ratio`(전경 비율)가 비정상(≈0 또는 ≈1)이면 `needs_review=true`로 표시 → 자동 검수 큐잉.

### 대량 처리 (기존 카탈로그 백필)

```bash
python -m scripts.batch /path/to/photos --model isnet-general-use
python -m scripts.batch ./incoming --watch        # 새 파일 자동 감시·처리
```

---

## 아키텍처

```
app/
  config.py    설정(.env) — 모델/저장소/DB/출력 옵션
  engine.py    누끼 엔진: rembg 세션 캐시, 동시성 제한, 투명PNG·흰배경JPG·썸네일 생성
  storage.py   저장소 어댑터: LocalStorage | S3Storage (동일 인터페이스)
  database.py  SQLAlchemy 엔진/세션 (SQLite 기본, DATABASE_URL로 Postgres)
  models.py    ImageAsset — 등록 레코드(상태·URL·크기·처리시간·품질지표)
  service.py   파이프라인: register → process → store → DB 등록
  main.py      FastAPI 라우트 + 테스트 UI + 정적 파일 서빙
scripts/
  evaluate.py  모델 벤치마크/비교 그리드 생성 (BENCHMARK.md 재현)
  batch.py     폴더 일괄 처리 + 감시 모드
tests/         pytest (엔진은 가짜로 대체해 빠르고 결정적)
```

처리는 기본 **동기**(1장 ~1초)이며, 대량·고품질 작업은 `?background=true`로 비동기 처리 후
`GET /api/v1/images/{id}` 폴링으로 결과를 받습니다.

---

## 플랫폼 연동

이 서비스는 특정 플랫폼에 종속되지 않는 **독립형**입니다. 연동 방법 3가지:

1. **업로드 프록시** — 플랫폼 업로드 폼이 이 API(`POST /api/v1/images`)로 먼저 보내고,
   돌아온 `cutout_url`/`flat_url`을 상품에 저장.
2. **배치 백필** — 기존 이미지 폴더/버킷을 `scripts/batch.py`로 일괄 변환.
3. **스토리지 직결** — `STORAGE_BACKEND=s3` + CDN(`S3_PUBLIC_BASE_URL`)으로 결과를 바로 서빙.

> 전자상거래(예: Shopify)·CMS 등으로의 자동 **게시(push)** 가 필요하면, `service.py`의 성공 지점에
> 훅을 추가해 해당 플랫폼 API로 `cutout_url`을 등록하면 됩니다. (요청 주시면 어댑터를 추가합니다.)

---

## 설정 (.env)

주요 항목만. 전체는 [`.env.example`](.env.example) 참고.

| 변수 | 기본 | 설명 |
|---|---|---|
| `REMBG_MODEL` | `isnet-general-use` | 누끼 모델 |
| `MAX_CONCURRENCY` | `2` | 동시 추론 수 (RAM 보호) |
| `ALPHA_MATTING` | `false` | 미세 경계(머리카락) 보정, 느려짐 |
| `FLAT_BACKGROUND` | `255,255,255` | 흰배경 JPG 합성색 |
| `MAX_OUTPUT_PX` | `2000` | 출력 긴 변 상한 |
| `STORAGE_BACKEND` | `local` | `local` \| `s3` |
| `DATABASE_URL` | (빈값→SQLite) | Postgres 등 |

---

## 라이선스 주의 (상업 사용)

`rembg` **코드는 MIT**지만 **모델 가중치는 각자 상위 라이선스**를 따릅니다. 상업 플랫폼에서는:

- ✅ **사용 가능 (MIT):** `isnet-general-use`, `birefnet-general`, `birefnet-general-lite`, `u2net`
- ⛔ **비상업 — 쓰지 말 것:** `bria-rmbg`(RMBG-2.0), `u2net-human-seg` 등
  → 상업용은 BRIA의 별도 유료 라이선스 필요.

본 시스템 기본값(`isnet-general-use`)은 상업 사용이 안전한 MIT 모델입니다. `REMBG_MODEL`을 바꿀 때
반드시 해당 모델의 라이선스를 확인하세요.

---

## 테스트

```bash
pip install -r requirements.txt
pytest -q
```

ONNX 엔진을 가짜로 대체하므로 모델 다운로드 없이 빠르게(수 초) 통과합니다.
```
