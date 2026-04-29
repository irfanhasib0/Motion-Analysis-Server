#pragma once

#include <opencv2/opencv.hpp>
#include <onnxruntime_cxx_api.h>

#include <memory>
#include <string>
#include <unordered_set>
#include <vector>

// Detection result — matches the Python dict structure.
struct Detection {
    int   x1, y1, x2, y2;   // bbox (pixel coords, clamped to frame)
    float cx, cy;            // centroid (row, col)
    float score;
    int   class_id;
    std::string class_name;
};

// C linkage for shared-library consumers that don't use C++.
#ifdef _WIN32
#  define YOLOX_API __declspec(dllexport)
#else
#  define YOLOX_API __attribute__((visibility("default")))
#endif

extern "C" {
    // Opaque handle
    typedef void* YOLOXHandle;

    // Create / destroy
    YOLOX_API YOLOXHandle yolox_create(const char* model_path,
                             int input_h, int input_w,
                             float score_thr, float nms_thr);
    YOLOX_API void        yolox_destroy(YOLOXHandle handle);

    // Enable / disable inference (disabled by default after create)
    YOLOX_API void yolox_set_enabled(YOLOXHandle handle, int enabled);
    YOLOX_API int  yolox_is_enabled(YOLOXHandle handle);

    // Detect — writes up to max_dets results into out[].
    // Returns the number of detections written (≤ max_dets), or -1 on error.
    // out must point to an array of at least max_dets Detection structs.
    YOLOX_API int  yolox_detect(YOLOXHandle handle,
                      const uint8_t* bgr_data, int frame_h, int frame_w,
                      Detection* out, int max_dets);

    // Last inference latency in milliseconds.
    YOLOX_API float yolox_last_latency_ms(YOLOXHandle handle);
}

// C++ class — use this directly from C++ callers.
class YOLOXDetector {
public:
    explicit YOLOXDetector(const std::string& model_path,
                           int input_h = 416, int input_w = 416,
                           float score_thr = 0.5f, float nms_thr = 0.45f,
                           std::unordered_set<int> target_class_ids = {0, 2});

    ~YOLOXDetector() = default;

    // Non-copyable, movable.
    YOLOXDetector(const YOLOXDetector&)            = delete;
    YOLOXDetector& operator=(const YOLOXDetector&) = delete;
    YOLOXDetector(YOLOXDetector&&)                 = default;
    YOLOXDetector& operator=(YOLOXDetector&&)      = default;

    void set_enabled(bool enabled) { enabled_ = enabled; }
    bool is_enabled()        const { return enabled_; }
    void set_score_threshold(float thr) { score_thr_ = thr; }
    void set_target_classes(std::unordered_set<int> ids) { target_class_ids_ = std::move(ids); }

    // Run detection on a BGR frame. Returns empty vector when disabled.
    std::vector<Detection> detect(const cv::Mat& frame_bgr);

    float last_latency_ms() const { return last_latency_ms_; }

private:
    // Pre/postprocessing helpers
    cv::Mat preproc(const cv::Mat& img, float& ratio) const;
    void    decode_outputs(float* data, int num_anchors, int num_classes,
                           float ratio,
                           int frame_h, int frame_w,
                           std::vector<Detection>& out) const;

    // NMS
    static std::vector<int> nms(const std::vector<std::array<float,4>>& boxes,
                                const std::vector<float>& scores,
                                float nms_thr);

    // ORT state
    Ort::Env            env_;
    Ort::Session        session_;
    Ort::AllocatorWithDefaultOptions allocator_;
    std::string         input_name_;
    std::string         output_name_;

    int   input_h_, input_w_;
    float score_thr_, nms_thr_;
    bool  enabled_ = false;
    float last_latency_ms_ = 0.0f;

    std::unordered_set<int> target_class_ids_;
};
