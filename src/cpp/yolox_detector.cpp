#include "yolox_detector.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <numeric>
#include <stdexcept>

// ---------------------------------------------------------------------------
// COCO class names (80 classes, index == COCO category ID)
// ---------------------------------------------------------------------------
static const char* COCO_CLASSES[] = {
    "person","bicycle","car","motorcycle","airplane","bus","train","truck",
    "boat","traffic light","fire hydrant","stop sign","parking meter","bench",
    "bird","cat","dog","horse","sheep","cow","elephant","bear","zebra",
    "giraffe","backpack","umbrella","handbag","tie","suitcase","frisbee",
    "skis","snowboard","sports ball","kite","baseball bat","baseball glove",
    "skateboard","surfboard","tennis racket","bottle","wine glass","cup",
    "fork","knife","spoon","bowl","banana","apple","sandwich","orange",
    "broccoli","carrot","hot dog","pizza","donut","cake","chair","couch",
    "potted plant","bed","dining table","toilet","tv","laptop","mouse",
    "remote","keyboard","cell phone","microwave","oven","toaster","sink",
    "refrigerator","book","clock","vase","scissors","teddy bear",
    "hair drier","toothbrush"
};
static constexpr int NUM_CLASSES = static_cast<int>(sizeof(COCO_CLASSES) / sizeof(COCO_CLASSES[0]));

// ---------------------------------------------------------------------------
// Grid strides — matches _demo_postprocess (strides 8, 16, 32)
// ---------------------------------------------------------------------------
struct GridStride { int grid_x, grid_y, stride; };

static std::vector<GridStride> generate_grid_strides(int input_h, int input_w) {
    static const int strides[] = {8, 16, 32};
    std::vector<GridStride> gs;
    for (int s : strides) {
        int gh = input_h / s, gw = input_w / s;
        for (int gy = 0; gy < gh; ++gy)
            for (int gx = 0; gx < gw; ++gx)
                gs.push_back({gx, gy, s});
    }
    return gs;
}

// ---------------------------------------------------------------------------
// YOLOXDetector — constructor
// ---------------------------------------------------------------------------
YOLOXDetector::YOLOXDetector(const std::string& model_path,
                              int input_h, int input_w,
                              float score_thr, float nms_thr,
                              std::unordered_set<int> target_class_ids)
    : env_(ORT_LOGGING_LEVEL_WARNING, "yolox")
    , session_([&]() {
        Ort::SessionOptions opts;
        const auto& providers = Ort::GetAvailableProviders();
        if (std::find(providers.begin(), providers.end(),
                      "XNNPACKExecutionProvider") != providers.end()) {
            opts.AppendExecutionProvider("XNNPACK", {});
            fprintf(stderr, "YOLOX: XNNPACKExecutionProvider detected — using XNNPACK\n");
        }
        return Ort::Session(env_, model_path.c_str(), opts);
      }())
    , input_h_(input_h), input_w_(input_w)
    , score_thr_(score_thr), nms_thr_(nms_thr)
    , target_class_ids_(std::move(target_class_ids))
{
    // Resolve input / output names from the model graph.
    {
        auto name_ptr = session_.GetInputNameAllocated(0, allocator_);
        input_name_   = name_ptr.get();
    }
    {
        auto name_ptr  = session_.GetOutputNameAllocated(0, allocator_);
        output_name_   = name_ptr.get();
    }

    enabled_ = true;
}

// ---------------------------------------------------------------------------
// preproc — letterbox resize, CHW float32, returns scale ratio
// ---------------------------------------------------------------------------
cv::Mat YOLOXDetector::preproc(const cv::Mat& img, float& ratio) const {
    ratio = std::min(static_cast<float>(input_h_) / img.rows,
                     static_cast<float>(input_w_) / img.cols);

    int new_h = static_cast<int>(img.rows * ratio);
    int new_w = static_cast<int>(img.cols * ratio);

    cv::Mat resized;
    cv::resize(img, resized, {new_w, new_h}, 0, 0, cv::INTER_LINEAR);

    // Pad to input_h × input_w with value 114
    cv::Mat padded(input_h_, input_w_, CV_8UC3, cv::Scalar(114, 114, 114));
    resized.copyTo(padded(cv::Rect(0, 0, new_w, new_h)));

    // HWC uint8 → CHW float32
    cv::Mat fp32;
    padded.convertTo(fp32, CV_32F);   // still HWC

    // Split into channels and interleave as CHW blob
    cv::Mat chw(1, input_h_ * input_w_ * 3, CV_32F);
    float* dst = chw.ptr<float>();
    std::vector<cv::Mat> planes(3);
    cv::split(fp32, planes);
    for (int c = 0; c < 3; ++c) {
        // planes[c] is a H×W float32 mat; copy into CHW order
        std::copy(planes[c].begin<float>(), planes[c].end<float>(),
                  dst + c * input_h_ * input_w_);
    }
    return chw;   // shape: (1, 3*H*W) — we'll reshape before ORT call
}

// ---------------------------------------------------------------------------
// decode_outputs — grid decode + sigmoid + NMS
// ---------------------------------------------------------------------------
void YOLOXDetector::decode_outputs(float* data, int num_anchors, int num_classes,
                                    float ratio,
                                    int frame_h, int frame_w,
                                    std::vector<Detection>& out) const {
    // data layout: [num_anchors, 5 + num_classes]
    //   [:, 0:2]  = (cx, cy) — grid-relative centre
    //   [:, 2:4]  = (w, h)   — log-scale
    //   [:, 4]    = objectness
    //   [:, 5:]   = class scores (all already sigmoid-applied by ONNX graph)

    auto grid_strides = generate_grid_strides(input_h_, input_w_);
    const int stride = 5 + num_classes;

    std::vector<std::array<float,4>> boxes;
    std::vector<float>               scores;
    std::vector<int>                 class_ids;

    boxes.reserve(num_anchors);
    scores.reserve(num_anchors);
    class_ids.reserve(num_anchors);

    for (int i = 0; i < num_anchors; ++i) {
        const float* row = data + i * stride;
        const float  obj = row[4];

        // Find best class score
        int   best_cls   = 0;
        float best_score = row[5];
        for (int c = 1; c < num_classes; ++c) {
            if (row[5 + c] > best_score) { best_score = row[5 + c]; best_cls = c; }
        }

        float conf = obj * best_score;
        if (conf < score_thr_) continue;
        if (target_class_ids_.count(best_cls) == 0) continue;

        // Grid decode (mirrors _demo_postprocess)
        const auto& gs = grid_strides[i];
        float cx = (row[0] + gs.grid_x) * gs.stride;
        float cy = (row[1] + gs.grid_y) * gs.stride;
        float bw = std::exp(row[2]) * gs.stride;
        float bh = std::exp(row[3]) * gs.stride;

        // Scale back to original image coords
        float x1 = (cx - bw * 0.5f) / ratio;
        float y1 = (cy - bh * 0.5f) / ratio;
        float x2 = (cx + bw * 0.5f) / ratio;
        float y2 = (cy + bh * 0.5f) / ratio;

        boxes.push_back({x1, y1, x2, y2});
        scores.push_back(conf);
        class_ids.push_back(best_cls);
    }

    if (boxes.empty()) return;

    auto keep = nms(boxes, scores, nms_thr_);

    for (int idx : keep) {
        const auto& b = boxes[idx];
        int x1 = std::max(0, static_cast<int>(b[0]));
        int y1 = std::max(0, static_cast<int>(b[1]));
        int x2 = std::min(frame_w, static_cast<int>(b[2]));
        int y2 = std::min(frame_h, static_cast<int>(b[3]));
        int bw = x2 - x1, bh = y2 - y1;
        if (bw <= 0 || bh <= 0) continue;

        Detection d;
        d.x1 = x1; d.y1 = y1; d.x2 = x2; d.y2 = y2;
        d.cy = y1 + bh * 0.5f;   // row centroid
        d.cx = x1 + bw * 0.5f;   // col centroid
        d.score    = scores[idx];
        d.class_id = class_ids[idx];
        d.class_name = (class_ids[idx] < NUM_CLASSES)
                       ? COCO_CLASSES[class_ids[idx]]
                       : std::to_string(class_ids[idx]);
        out.push_back(d);
    }
}

// ---------------------------------------------------------------------------
// NMS — mirrors Python _nms()
// ---------------------------------------------------------------------------
std::vector<int> YOLOXDetector::nms(const std::vector<std::array<float,4>>& boxes,
                                     const std::vector<float>& scores,
                                     float nms_thr) {
    std::vector<int> order(scores.size());
    std::iota(order.begin(), order.end(), 0);
    std::sort(order.begin(), order.end(),
              [&](int a, int b){ return scores[a] > scores[b]; });

    std::vector<bool> suppressed(scores.size(), false);
    std::vector<int>  keep;

    for (int ii = 0; ii < static_cast<int>(order.size()); ++ii) {
        int i = order[ii];
        if (suppressed[i]) continue;
        keep.push_back(i);

        float x1i = boxes[i][0], y1i = boxes[i][1];
        float x2i = boxes[i][2], y2i = boxes[i][3];
        float ai   = (x2i - x1i + 1) * (y2i - y1i + 1);

        for (int jj = ii + 1; jj < static_cast<int>(order.size()); ++jj) {
            int j = order[jj];
            if (suppressed[j]) continue;

            float xx1 = std::max(x1i, boxes[j][0]);
            float yy1 = std::max(y1i, boxes[j][1]);
            float xx2 = std::min(x2i, boxes[j][2]);
            float yy2 = std::min(y2i, boxes[j][3]);
            float iw  = std::max(0.0f, xx2 - xx1 + 1);
            float ih  = std::max(0.0f, yy2 - yy1 + 1);
            float inter = iw * ih;

            float aj  = (boxes[j][2] - boxes[j][0] + 1) * (boxes[j][3] - boxes[j][1] + 1);
            float iou = inter / (ai + aj - inter);
            if (iou > nms_thr) suppressed[j] = true;
        }
    }
    return keep;
}

// ---------------------------------------------------------------------------
// detect()
// ---------------------------------------------------------------------------
std::vector<Detection> YOLOXDetector::detect(const cv::Mat& frame_bgr) {
    if (!enabled_) return {};

    auto t0 = std::chrono::steady_clock::now();

    // Preprocess
    float ratio;
    cv::Mat chw = preproc(frame_bgr, ratio);

    // Build ORT input tensor: shape [1, 3, H, W]
    std::array<int64_t, 4> input_shape{1, 3, input_h_, input_w_};
    Ort::MemoryInfo mem_info = Ort::MemoryInfo::CreateCpu(
        OrtArenaAllocator, OrtMemTypeDefault);

    Ort::Value input_tensor = Ort::Value::CreateTensor<float>(
        mem_info,
        chw.ptr<float>(),
        static_cast<size_t>(3 * input_h_ * input_w_),
        input_shape.data(), input_shape.size());

    const char* input_names[]  = {input_name_.c_str()};
    const char* output_names[] = {output_name_.c_str()};

    auto outputs = session_.Run(
        Ort::RunOptions{nullptr},
        input_names,  &input_tensor, 1,
        output_names, 1);

    // Output: [1, N, 85] or [N, 85]
    auto& out_tensor   = outputs[0];
    auto  shape        = out_tensor.GetTensorTypeAndShapeInfo().GetShape();
    float* raw         = out_tensor.GetTensorMutableData<float>();

    int num_anchors, num_classes;
    if (shape.size() == 3) {
        // [1, N, C]
        num_anchors  = static_cast<int>(shape[1]);
        num_classes  = static_cast<int>(shape[2]) - 5;
    } else {
        // [N, C]
        num_anchors  = static_cast<int>(shape[0]);
        num_classes  = static_cast<int>(shape[1]) - 5;
    }

    std::vector<Detection> detections;
    decode_outputs(raw, num_anchors, num_classes,
                   ratio, frame_bgr.rows, frame_bgr.cols,
                   detections);

    auto t1 = std::chrono::steady_clock::now();
    last_latency_ms_ = std::chrono::duration<float, std::milli>(t1 - t0).count();

    return detections;
}

// ---------------------------------------------------------------------------
// C API
// ---------------------------------------------------------------------------
extern "C" {

YOLOXHandle yolox_create(const char* model_path,
                          int input_h, int input_w,
                          float score_thr, float nms_thr) {
    try {
        auto* det = new YOLOXDetector(model_path, input_h, input_w,
                                       score_thr, nms_thr);
        return det;
    } catch (...) {
        return nullptr;
    }
}

void yolox_destroy(YOLOXHandle handle) {
    delete static_cast<YOLOXDetector*>(handle);
}

void yolox_set_enabled(YOLOXHandle handle, int enabled) {
    static_cast<YOLOXDetector*>(handle)->set_enabled(enabled != 0);
}

int yolox_is_enabled(YOLOXHandle handle) {
    return static_cast<YOLOXDetector*>(handle)->is_enabled() ? 1 : 0;
}

int yolox_detect(YOLOXHandle handle,
                  const uint8_t* bgr_data, int frame_h, int frame_w,
                  Detection* out, int max_dets) {
    try {
        auto* det = static_cast<YOLOXDetector*>(handle);
        // Wrap raw pointer in a cv::Mat — no copy.
        cv::Mat frame(frame_h, frame_w, CV_8UC3,
                      const_cast<uint8_t*>(bgr_data));
        auto results = det->detect(frame);
        int n = std::min(static_cast<int>(results.size()), max_dets);
        for (int i = 0; i < n; ++i) out[i] = results[i];
        return n;
    } catch (...) {
        return -1;
    }
}

float yolox_last_latency_ms(YOLOXHandle handle) {
    return static_cast<YOLOXDetector*>(handle)->last_latency_ms();
}

} // extern "C"
