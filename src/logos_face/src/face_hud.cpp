#include <ros/ros.h>
#include <std_msgs/String.h>

#include <logos_msgs/EyeGazeX.h>
#include <logos_msgs/EyeGazeY.h>
#include <logos_msgs/EyeScaleX.h>
#include <logos_msgs/EyeScaleY.h>
#include <logos_msgs/EyeLidHeight.h>
#include <logos_msgs/EyeLidAngle.h>
#include <logos_msgs/EyeColor.h>
#include <logos_msgs/MouthSine.h>
#include <logos_msgs/AudioWave.h>

#include <cv_bridge/cv_bridge.h>
#include <opencv2/opencv.hpp>
#include <sensor_msgs/Image.h>
#include <sensor_msgs/image_encodings.h>

#include <dynamic_reconfigure/server.h>
#include <logos_face/FaceNodeConfig.h>

#include <caca.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cctype>
#include <deque>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include <boost/property_tree/json_parser.hpp>
#include <boost/property_tree/ptree.hpp>

#include <sys/ioctl.h>
#include <sys/select.h>
#include <termios.h>
#include <unistd.h>

// Keyboard controls
// q = quit
// r = re-detect terminal size (ANSI mode only)
// a/d = decrease/increase columns (ANSI mode only)
// s/w = decrease/increase rows (ANSI mode only)
// [/]= decrease/increase fps
// -/+ = decrease/increase upper face pane height
// \ = clear screen
const char KEY_QUIT = 'q';
const char KEY_RESET = 'r';
const char KEY_INCREASE_COLS = 'd';
const char KEY_DECREASE_COLS = 'a';
const char KEY_INCREASE_ROWS = 'w';
const char KEY_DECREASE_ROWS = 's';
const char KEY_INCREASE_FPS = ']';
const char KEY_DECREASE_FPS = '[';
const char KEY_INCREASE_FACE_PANE = '+';
const char KEY_INCREASE_FACE_PANE_ALT = '=';
const char KEY_DECREASE_FACE_PANE = '-';
const char KEY_CLEAR_SCREEN = '\\';
const double PANE_RATIO_STEP = 0.01;

struct AnimParam {
    double start_value;
    double end_value;
    double duration;
    ros::Time start_time;
    bool active;

    AnimParam() : start_value(0.0), end_value(0.0), duration(0.0), active(false) {}
};

struct ColorAnimParam {
    cv::Vec3b start_rgb;
    cv::Vec3b end_rgb;
    double duration;
    ros::Time start_time;
    bool active;

    ColorAnimParam() : duration(0.0), active(false) {}
};

class FaceNodeCpp {
public:
    FaceNodeCpp() : nh_("~"), quit_requested_(false) {
        terminal_cols_ = 24;
        terminal_rows_ = 24;

        fps_ = nh_.param<int>("fps", 8);
        min_fps_ = 1;
        max_fps_ = 24;

        output_mode_ = nh_.param<std::string>("output_mode", "display");
        caca_driver_ = nh_.param<std::string>("caca_driver", "ncurses");

        dither_antialias_ = nh_.param<std::string>("dither_antialias", "default");
        dither_color_ = nh_.param<std::string>("dither_color", "full16");
        dither_charset_ = nh_.param<std::string>("dither_charset", "ascii");
        dither_algorithm_ = nh_.param<std::string>("dither_algorithm", "ordered4");

        // TWEAKABLE RENDER PARAMS: terminal raster density before libcaca dithering.
        render_px_per_char_x_ = std::max(0.1, nh_.param<double>("render_px_per_char_x", 1.0));
        render_px_per_char_y_ = std::max(0.1, nh_.param<double>("render_px_per_char_y", 1.0));

        // TWEAKABLE RENDER PARAMS: eye placement and scale ratios.
        eye_center_y_ratio_ = clampDouble(nh_.param<double>("eye_center_y_ratio", 0.375), 0.0, 1.0);
        eye_gaze_x_ratio_ = std::max(0.0, nh_.param<double>("eye_gaze_x_ratio", 0.25));
        eye_gaze_y_ratio_ = std::max(0.0, nh_.param<double>("eye_gaze_y_ratio", 0.125));
        eye_radius_x_ratio_ = std::max(0.001, nh_.param<double>("eye_radius_x_ratio", 0.20));
        eye_radius_y_ratio_ = std::max(0.001, nh_.param<double>("eye_radius_y_ratio", 0.20));
        eye_outline_thickness_px_ = std::max(1, nh_.param<int>("eye_outline_thickness_px", 2));

        // TWEAKABLE RENDER PARAMS: lid/brow line and the restored area above it.
        eye_lid_height_ratio_ = std::max(0.0, nh_.param<double>("eye_lid_height_ratio", 0.25));
        eye_lid_thickness_ratio_ = std::max(0.0, nh_.param<double>("eye_lid_thickness_ratio", 0.02));
        eye_lid_min_thickness_px_ = std::max(1, nh_.param<int>("eye_lid_min_thickness_px", 1));
        eye_lid_erase_padding_x_ratio_ = std::max(0.0, nh_.param<double>("eye_lid_erase_padding_x_ratio", 0.025));

        // TWEAKABLE RENDER PARAMS: mouth/audio waveform placement and thickness.
        waveform_baseline_y_ratio_ = clampDouble(nh_.param<double>("waveform_baseline_y_ratio", 0.875), 0.0, 1.0);
        waveform_amplitude_y_ratio_ = std::max(0.0, nh_.param<double>("waveform_amplitude_y_ratio", 0.125));
        audio_wave_thickness_ratio_ = std::max(0.0, nh_.param<double>("audio_wave_thickness_ratio", 1.0 / 70.0));
        mouth_sine_thickness_ = std::max(1, nh_.param<int>("mouth_sine_thickness", 4));

        hud_event_topic_ = nh_.param<std::string>("hud_event_topic", "/face/hud/event");
        // TWEAKABLE HUD PARAMS: pane split, default colors, retained line counts, and figlet fonts.
        double default_status_region_ratio = 0.33;
        nh_.getParam("caption_region_ratio", default_status_region_ratio);
        status_region_ratio_ = clampDouble(nh_.param<double>("status_region_ratio", default_status_region_ratio), 0.05, 0.95);
        face_default_color_ = colorNameToCaca(nh_.param<std::string>("face_canvas_color", "bright_green"));
        status_default_color_ = colorNameToCaca(nh_.param<std::string>("status_color", "bright_white"));
        caption_default_color_ = colorNameToCaca(nh_.param<std::string>("caption_color", "bright_magenta"));
        hud_bg_color_ = colorNameToCaca(nh_.param<std::string>("hud_bg_color", "black"));
        face_max_lines_ = std::max(1, nh_.param<int>("face_max_lines", 240));
        status_max_lines_ = std::max(1, nh_.param<int>("status_max_lines", 120));
        default_figlet_font_ = nh_.param<std::string>("default_figlet_font", "standard");
        caption_figlet_font_ = nh_.param<std::string>("caption_figlet_font", "thick");

        layer0_image_topic_ = nh_.param<std::string>("layer0_image_topic", "/face/layer0/image");
        layer2_image_topic_ = nh_.param<std::string>("layer2_image_topic", "/face/layer2/image");
        // TWEAKABLE IMAGE PARAMS: fade envelope for /face/layer0/image and /face/layer2/image.
        layer_image_fade_in_sec_ = std::max(0.0, nh_.param<double>("layer_image_fade_in_sec", nh_.param<double>("debug_image_fade_in_sec", 0.6)));
        layer_image_hold_sec_ = std::max(0.0, nh_.param<double>("layer_image_hold_sec", nh_.param<double>("debug_image_hold_sec", 4.0)));
        layer_image_fade_out_sec_ = std::max(0.0, nh_.param<double>("layer_image_fade_out_sec", nh_.param<double>("debug_image_fade_out_sec", 0.8)));
        layer_image_max_alpha_ = clampDouble(nh_.param<double>("layer_image_max_alpha", nh_.param<double>("debug_image_max_alpha", 1.0)), 0.0, 1.0);

        min_render_width_ = 16;
        min_render_height_ = 16;
        render_width_ = 200;
        render_height_ = 200;

        getTerminalSize();

        initEyeParams();
        initAudioParams();
        initEffectParams();
        initAmplitudeColorLut();

        sub_gaze_x_ = nh_.subscribe("/face/eye_gaze_x", 10, &FaceNodeCpp::gazeXCallback, this);
        sub_gaze_y_ = nh_.subscribe("/face/eye_gaze_y", 10, &FaceNodeCpp::gazeYCallback, this);
        sub_scale_x_ = nh_.subscribe("/face/eye_scale_x", 10, &FaceNodeCpp::scaleXCallback, this);
        sub_scale_y_ = nh_.subscribe("/face/eye_scale_y", 10, &FaceNodeCpp::scaleYCallback, this);
        sub_lid_height_ = nh_.subscribe("/face/eye_lid_height", 10, &FaceNodeCpp::lidHeightCallback, this);
        sub_lid_angle_ = nh_.subscribe("/face/eye_lid_angle", 10, &FaceNodeCpp::lidAngleCallback, this);
        sub_color_ = nh_.subscribe("/face/eye_color", 10, &FaceNodeCpp::colorCallback, this);
        sub_mouth_sine_ = nh_.subscribe("/face/mouth/sine_wave", 10, &FaceNodeCpp::sineWaveCallback, this);
        sub_audio_wave_ = nh_.subscribe("/face/mouth/audio_wave", 10, &FaceNodeCpp::audioWaveCallback, this);
        sub_hud_event_ = nh_.subscribe(hud_event_topic_, 50, &FaceNodeCpp::hudEventCallback, this);
        sub_layer0_image_ = nh_.subscribe(layer0_image_topic_, 1, &FaceNodeCpp::layer0ImageCallback, this);
        sub_layer2_image_ = nh_.subscribe(layer2_image_topic_, 1, &FaceNodeCpp::layer2ImageCallback, this);

        pub_live_state_json_ = nh_.advertise<std_msgs::String>("/face/live_state/json", 10);

        {
            std::lock_guard<std::mutex> lock(param_mutex_);
            initCacaLocked();
            ensureRenderGeometryLocked();
        }

        dyn_srv_.setCallback(boost::bind(&FaceNodeCpp::configCallback, this, _1, _2));

        updateRenderTimer();
        hud_event_thread_ = std::thread(&FaceNodeCpp::hudEventWorker, this);

        if (!using_caca_display_) {
            setupTerminal();
            keypress_thread_ = std::thread(&FaceNodeCpp::keypressListener, this);
        }
    }

    ~FaceNodeCpp() {
        quit_requested_ = true;

        {
            std::lock_guard<std::mutex> lock(hud_event_mutex_);
            hud_event_stop_ = true;
        }
        hud_event_cv_.notify_all();
        if (hud_event_thread_.joinable()) {
            hud_event_thread_.join();
        }

        if (keypress_thread_.joinable()) {
            keypress_thread_.join();
        }

        if (!using_caca_display_) {
            restoreTerminal();
        }

        std::lock_guard<std::mutex> lock(param_mutex_);
        shutdownCacaLocked();
    }

    void run() {
        ros::spin();
    }

private:
    struct EyeParams {
        double gaze_x;
        double gaze_y;
        double scale_x;
        double scale_y;
        double lid_height;
        double lid_angle;
        std::string color;
    };

    struct EffectParams {
        double frequency;
        double amplitude;
        double phase;
        double phase_increment;
        std::string color;
    };

    struct HudLine {
        std::string text;
        uint8_t fg;
        uint8_t bg;
        ros::Time expires_at;
    };

    struct StatusPrintJob {
        std::vector<HudLine> lines;
        ros::Time start_time;
        double duration;
        size_t next_line_index;
        bool caption;
        bool started;
    };

    struct FaceCrawlEffect {
        std::vector<HudLine> lines;
        ros::Time start_time;
        double speed = 8.0;
        double duration = 0.0;
        bool active = false;
    };

    struct FaceRainEffect {
        std::string chars;
        ros::Time start_time;
        double speed = 8.0;
        double duration = 0.0;
        double density = 0.18;
        uint8_t fg = CACA_LIGHTGREEN;
        uint8_t bg = CACA_BLACK;
        bool active = false;
    };

    struct FaceLayerState {
        std::deque<HudLine> terminal_lines;
        FaceCrawlEffect crawl;
        FaceRainEffect rain;
    };

    struct LayerImageState {
        cv::Mat image_bgr;
        cv::Mat resized_bgr;
        ros::Time start_time;
        bool active = false;
    };

    ros::NodeHandle nh_;

    ros::Subscriber sub_gaze_x_;
    ros::Subscriber sub_gaze_y_;
    ros::Subscriber sub_scale_x_;
    ros::Subscriber sub_scale_y_;
    ros::Subscriber sub_lid_height_;
    ros::Subscriber sub_lid_angle_;
    ros::Subscriber sub_color_;
    ros::Subscriber sub_mouth_sine_;
    ros::Subscriber sub_audio_wave_;
    ros::Subscriber sub_hud_event_;
    ros::Subscriber sub_layer0_image_;
    ros::Subscriber sub_layer2_image_;

    ros::Publisher pub_live_state_json_;

    dynamic_reconfigure::Server<logos_face::FaceNodeConfig> dyn_srv_;
    ros::Timer render_timer_;

    std::mutex param_mutex_;
    std::mutex hud_event_mutex_;
    std::condition_variable hud_event_cv_;
    std::deque<std::string> hud_event_queue_;
    bool hud_event_stop_ = false;
    std::atomic<bool> quit_requested_;

    int terminal_cols_;
    int terminal_rows_;

    int fps_;
    int min_fps_;
    int max_fps_;

    std::string output_mode_;
    std::string caca_driver_;
    std::string dither_antialias_;
    std::string dither_color_;
    std::string dither_charset_;
    std::string dither_algorithm_;

    double render_px_per_char_x_;
    double render_px_per_char_y_;
    int render_width_;
    int render_height_;
    int min_render_width_;
    int min_render_height_;
    double eye_center_y_ratio_;
    double eye_gaze_x_ratio_;
    double eye_gaze_y_ratio_;
    double eye_radius_x_ratio_;
    double eye_radius_y_ratio_;
    int eye_outline_thickness_px_;
    double eye_lid_height_ratio_;
    double eye_lid_thickness_ratio_;
    int eye_lid_min_thickness_px_;
    double eye_lid_erase_padding_x_ratio_;
    double waveform_baseline_y_ratio_;
    double waveform_amplitude_y_ratio_;
    double audio_wave_thickness_ratio_;
    int mouth_sine_thickness_;

    std::string hud_event_topic_;
    std::string layer0_image_topic_;
    std::string layer2_image_topic_;
    std::array<FaceLayerState, 2> face_layers_;
    std::deque<HudLine> status_lines_;
    std::deque<StatusPrintJob> status_print_jobs_;
    double status_region_ratio_;
    uint8_t face_default_color_;
    uint8_t status_default_color_;
    uint8_t caption_default_color_;
    uint8_t hud_bg_color_;
    int face_max_lines_;
    int status_max_lines_;
    std::string default_figlet_font_;
    std::string caption_figlet_font_;

    std::array<LayerImageState, 2> layer_images_;
    double layer_image_fade_in_sec_;
    double layer_image_hold_sec_;
    double layer_image_fade_out_sec_;
    double layer_image_max_alpha_;

    caca_display_t* caca_display_ = nullptr;
    caca_canvas_t* caca_canvas_ = nullptr;
    caca_dither_t* caca_dither_ = nullptr;
    bool using_caca_display_ = false;

    cv::Mat frame_bgr_;
    cv::Mat rgba_;
    cv::Mat black_mask_;
    cv::Mat alpha_;

    EyeParams current_left_;
    EyeParams current_right_;
    EyeParams start_left_;
    EyeParams start_right_;
    EyeParams target_left_;
    EyeParams target_right_;

    AnimParam left_gaze_x_anim_;
    AnimParam left_gaze_y_anim_;
    AnimParam left_scale_x_anim_;
    AnimParam left_scale_y_anim_;
    AnimParam left_lid_height_anim_;
    AnimParam left_lid_angle_anim_;
    ColorAnimParam left_color_anim_;

    AnimParam right_gaze_x_anim_;
    AnimParam right_gaze_y_anim_;
    AnimParam right_scale_x_anim_;
    AnimParam right_scale_y_anim_;
    AnimParam right_lid_height_anim_;
    AnimParam right_lid_angle_anim_;
    ColorAnimParam right_color_anim_;

    std::vector<float> audio_wave_;
    double audio_sample_rate_;
    ros::Time audio_start_time_;
    double audio_duration_;

    EffectParams effect_params_;
    EffectParams effect_start_params_;
    EffectParams effect_target_params_;

    AnimParam effect_freq_anim_;
    AnimParam effect_amp_anim_;
    AnimParam effect_phase_anim_;
    AnimParam effect_pinc_anim_;
    ColorAnimParam effect_color_anim_;

    double effect_animation_duration_;
    ros::Time effect_animation_start_;

    std::vector<float> sine_wave_buffer_;
    std::vector<float> audio_buffer_;
    std::vector<float> combined_wave_;

    std::array<cv::Scalar, 256> amplitude_color_lut_;

    termios orig_settings_;
    std::thread keypress_thread_;
    std::thread hud_event_thread_;

    static double clampDouble(double value, double lo, double hi) {
        return std::max(lo, std::min(hi, value));
    }

    static int clampInt(int value, int lo, int hi) {
        return std::max(lo, std::min(hi, value));
    }

    static std::string normalizeHexColor(const std::string& color) {
        if (color.size() >= 7 && color[0] == '#') {
            return color.substr(0, 7);
        }
        return "#00FF00";
    }

    static std::string jsonEscape(const std::string& input) {
        std::ostringstream oss;
        for (const char c : input) {
            switch (c) {
                case '\"':
                    oss << "\\\"";
                    break;
                case '\\':
                    oss << "\\\\";
                    break;
                case '\b':
                    oss << "\\b";
                    break;
                case '\f':
                    oss << "\\f";
                    break;
                case '\n':
                    oss << "\\n";
                    break;
                case '\r':
                    oss << "\\r";
                    break;
                case '\t':
                    oss << "\\t";
                    break;
                default:
                    if (static_cast<unsigned char>(c) < 0x20) {
                        oss << "\\u"
                            << std::hex << std::setw(4) << std::setfill('0')
                            << static_cast<int>(static_cast<unsigned char>(c));
                    } else {
                        oss << c;
                    }
                    break;
            }
        }
        return oss.str();
    }

    static std::string toLower(std::string value) {
        std::transform(value.begin(), value.end(), value.begin(), [](unsigned char ch) {
            return static_cast<char>(std::tolower(ch));
        });
        return value;
    }

    static uint8_t colorNameToCaca(const std::string& color) {
        const std::string value = toLower(color);

        if (value == "black") return CACA_BLACK;
        if (value == "blue") return CACA_BLUE;
        if (value == "green") return CACA_GREEN;
        if (value == "cyan") return CACA_CYAN;
        if (value == "red") return CACA_RED;
        if (value == "magenta") return CACA_MAGENTA;
        if (value == "brown" || value == "yellow") return CACA_BROWN;
        if (value == "bright_black") return CACA_DARKGRAY;
        if (value == "bright_blue") return CACA_LIGHTBLUE;
        if (value == "bright_green") return CACA_LIGHTGREEN;
        if (value == "bright_cyan") return CACA_LIGHTCYAN;
        if (value == "bright_red") return CACA_LIGHTRED;
        if (value == "bright_magenta") return CACA_LIGHTMAGENTA;
        if (value == "bright_yellow") return CACA_YELLOW;
        if (value == "bright_white") return CACA_WHITE;
        if (value == "lightgray" || value == "lightgrey" || value == "gray" || value == "grey") {
            return CACA_LIGHTGRAY;
        }
        if (value == "darkgray" || value == "darkgrey") return CACA_DARKGRAY;
        if (value == "lightblue") return CACA_LIGHTBLUE;
        if (value == "lightgreen") return CACA_LIGHTGREEN;
        if (value == "lightcyan") return CACA_LIGHTCYAN;
        if (value == "lightred") return CACA_LIGHTRED;
        if (value == "lightmagenta") return CACA_LIGHTMAGENTA;
        if (value == "lightyellow") return CACA_YELLOW;
        if (value == "white") return CACA_WHITE;
        if (value == "default") return CACA_DEFAULT;

        return CACA_LIGHTGRAY;
    }

    static std::vector<std::string> splitLines(const std::string& text) {
        std::vector<std::string> lines;
        std::stringstream ss(text);
        std::string line;

        while (std::getline(ss, line)) {
            if (!line.empty() && line.back() == '\r') {
                line.pop_back();
            }
            lines.push_back(line);
        }

        if (lines.empty()) {
            lines.push_back(text);
        }

        return lines;
    }

    void initEyeParams() {
        current_left_ = {0.0, 0.0, 1.0, 1.0, 1.0, 0.0, "#00FF00"};
        current_right_ = {0.0, 0.0, 1.0, 1.0, 0.5, 0.0, "#00FF00"};

        start_left_ = current_left_;
        start_right_ = current_right_;
        target_left_ = current_left_;
        target_right_ = current_right_;
    }

    void initAudioParams() {
        audio_sample_rate_ = 22050.0;
        audio_duration_ = 0.0;
    }

    void initEffectParams() {
        effect_params_ = {1.0, 1.0, 0.0, 0.1, "#00FF00"};
        effect_start_params_ = effect_params_;
        effect_target_params_ = effect_params_;
        effect_animation_duration_ = 0.0;
    }

    void initAmplitudeColorLut() {
        for (int i = 0; i < 256; ++i) {
            // OpenCV hue range is 0..179.
            // 135 is violet-ish, 0 is red.
            // This makes index 0 = violet, index 255 = red.
            const float t = static_cast<float>(i) / 255.0f;
            const float hue = 135.0f * (1.0f - t);

            cv::Mat hsv(1, 1, CV_8UC3, cv::Scalar(hue, 255, 255));
            cv::Mat bgr;
            cv::cvtColor(hsv, bgr, cv::COLOR_HSV2BGR);

            const cv::Vec3b c = bgr.at<cv::Vec3b>(0, 0);
            amplitude_color_lut_[i] = cv::Scalar(c[0], c[1], c[2]);
        }
    }

    void getTerminalSize() {
        struct winsize w;
        if (ioctl(STDOUT_FILENO, TIOCGWINSZ, &w) == 0) {
            terminal_rows_ = w.ws_row;
            terminal_cols_ = w.ws_col;
        }

        if (terminal_rows_ < 4) {
            terminal_rows_ = 20;
        }
        if (terminal_cols_ < 4) {
            terminal_cols_ = 20;
        }
    }

    void configCallback(logos_face::FaceNodeConfig& config, uint32_t) {
        std::lock_guard<std::mutex> lock(param_mutex_);

        if (config.fps != fps_) {
            fps_ = config.fps;
            updateRenderTimer();
        }

        bool need_reinit_output = false;
        bool geometry_might_change = false;

        if (config.output_mode != output_mode_) {
            output_mode_ = config.output_mode;
            need_reinit_output = true;
        }

        if (config.caca_driver != caca_driver_) {
            caca_driver_ = config.caca_driver;
            need_reinit_output = true;
        }

        if (config.dither_antialias != dither_antialias_) {
            dither_antialias_ = config.dither_antialias;
            applyDitherSettingsLocked();
        }

        if (config.dither_color != dither_color_) {
            dither_color_ = config.dither_color;
            applyDitherSettingsLocked();
        }

        if (config.dither_charset != dither_charset_) {
            dither_charset_ = config.dither_charset;
            applyDitherSettingsLocked();
        }

        if (config.dither_algorithm != dither_algorithm_) {
            dither_algorithm_ = config.dither_algorithm;
            applyDitherSettingsLocked();
        }

        if (std::abs(config.render_px_per_char_x - render_px_per_char_x_) > 1e-6) {
            render_px_per_char_x_ = std::max(0.1, static_cast<double>(config.render_px_per_char_x));
            geometry_might_change = true;
        }

        if (std::abs(config.render_px_per_char_y - render_px_per_char_y_) > 1e-6) {
            render_px_per_char_y_ = std::max(0.1, static_cast<double>(config.render_px_per_char_y));
            geometry_might_change = true;
        }

        if (need_reinit_output) {
            shutdownCacaLocked();
            initCacaLocked();

            ROS_WARN_STREAM(
                "Output mode/driver changed. Restart node if keyboard behavior is wrong."
            );

            geometry_might_change = true;
        }

        if (geometry_might_change) {
            ensureRenderGeometryLocked();
        }
    }

    void updateRenderTimer() {
        if (render_timer_) {
            render_timer_.stop();
        }

        render_timer_ = nh_.createTimer(
            ros::Duration(1.0 / static_cast<double>(fps_)),
            &FaceNodeCpp::renderCallback,
            this
        );
    }

    void initCacaLocked() {
        using_caca_display_ = (output_mode_ == "display");

        if (using_caca_display_) {
            caca_display_ = caca_create_display_with_driver(nullptr, caca_driver_.c_str());
            if (!caca_display_) {
                ROS_ERROR_STREAM(
                    "Failed to create libcaca display with driver '"
                    << caca_driver_
                    << "'. Falling back to ANSI stdout."
                );
                using_caca_display_ = false;
            } else {
                caca_canvas_ = caca_get_canvas(caca_display_);
            }
        }

        if (!using_caca_display_) {
            caca_canvas_ = caca_create_canvas(terminal_cols_, std::max(1, terminal_rows_ - 1));
        }

        if (!caca_canvas_) {
            ROS_FATAL("Failed to create libcaca canvas.");
            ros::shutdown();
            return;
        }

        recreateRenderBuffersLocked(render_width_, render_height_);

        if (!using_caca_display_) {
            std::printf("\033[2J\033[H");
            std::fflush(stdout);
        }
    }

    void shutdownCacaLocked() {
        if (caca_dither_) {
            caca_free_dither(caca_dither_);
            caca_dither_ = nullptr;
        }

        if (using_caca_display_) {
            if (caca_display_) {
                caca_free_display(caca_display_);
                caca_display_ = nullptr;
            }
            caca_canvas_ = nullptr;
        } else {
            if (caca_canvas_) {
                caca_free_canvas(caca_canvas_);
                caca_canvas_ = nullptr;
            }
        }

        using_caca_display_ = false;
    }

    void recreateRenderBuffersLocked(int width, int height) {
        render_width_ = std::max(min_render_width_, width);
        render_height_ = std::max(min_render_height_, height);

        frame_bgr_.create(render_height_, render_width_, CV_8UC3);
        rgba_.create(render_height_, render_width_, CV_8UC4);
        black_mask_.create(render_height_, render_width_, CV_8UC1);
        alpha_.create(render_height_, render_width_, CV_8UC1);

        if (caca_dither_) {
            caca_free_dither(caca_dither_);
            caca_dither_ = nullptr;
        }

        caca_dither_ = caca_create_dither(
            32,
            render_width_,
            render_height_,
            render_width_ * 4,
            0x000000ff,
            0x0000ff00,
            0x00ff0000,
            0xff000000
        );

        if (!caca_dither_) {
            ROS_FATAL("Failed to create libcaca dither.");
            ros::shutdown();
            return;
        }

        applyDitherSettingsLocked();
    }

    void ensureRenderGeometryLocked() {
        if (!caca_canvas_) {
            return;
        }

        const int canvas_w = std::max(1, caca_get_canvas_width(caca_canvas_));
        const int canvas_h = std::max(1, caca_get_canvas_height(caca_canvas_));
        const int face_canvas_h = facePaneHeightForCanvas(canvas_h);

        const int target_w = std::max(
            min_render_width_,
            static_cast<int>(std::lround(canvas_w * render_px_per_char_x_))
        );

        const int target_h = std::max(
            min_render_height_,
            static_cast<int>(std::lround(face_canvas_h * render_px_per_char_y_))
        );

        if (target_w != render_width_ || target_h != render_height_) {
            recreateRenderBuffersLocked(target_w, target_h);
        }
    }

    void applyDitherSettingsLocked() {
        if (!caca_dither_) {
            return;
        }

        caca_set_dither_antialias(caca_dither_, dither_antialias_.c_str());
        caca_set_dither_color(caca_dither_, dither_color_.c_str());
        caca_set_dither_charset(caca_dither_, dither_charset_.c_str());
        caca_set_dither_algorithm(caca_dither_, dither_algorithm_.c_str());
    }

    int statusPaneHeightForCanvas(int canvas_h) const {
        if (canvas_h <= 1) {
            return 0;
        }

        const int status_h = clampInt(
            static_cast<int>(std::lround(static_cast<double>(canvas_h) * status_region_ratio_)),
            1,
            canvas_h - 1
        );

        return status_h;
    }

    int facePaneHeightForCanvas(int canvas_h) const {
        if (canvas_h <= 1) {
            return 1;
        }

        return clampInt(canvas_h - statusPaneHeightForCanvas(canvas_h), 1, canvas_h - 1);
    }

    void adjustFacePaneRatioLocked(double face_delta) {
        const double current_face_ratio = 1.0 - status_region_ratio_;
        const double next_face_ratio = clampDouble(current_face_ratio + face_delta, 0.01, 0.95);
        status_region_ratio_ = 1.0 - next_face_ratio;
        ensureRenderGeometryLocked();
    }

    void gazeXCallback(const logos_msgs::EyeGazeX::ConstPtr& msg) {
        updateTargetParam(msg->eye_side, "gaze_x", msg->gaze_x, msg->duration);
    }

    void gazeYCallback(const logos_msgs::EyeGazeY::ConstPtr& msg) {
        updateTargetParam(msg->eye_side, "gaze_y", msg->gaze_y, msg->duration);
    }

    void scaleXCallback(const logos_msgs::EyeScaleX::ConstPtr& msg) {
        updateTargetParam(msg->eye_side, "scale_x", msg->scale_x, msg->duration);
    }

    void scaleYCallback(const logos_msgs::EyeScaleY::ConstPtr& msg) {
        updateTargetParam(msg->eye_side, "scale_y", msg->scale_y, msg->duration);
    }

    void lidHeightCallback(const logos_msgs::EyeLidHeight::ConstPtr& msg) {
        updateTargetParam(msg->eye_side, "lid_height", msg->lid_height, msg->duration);
    }

    void lidAngleCallback(const logos_msgs::EyeLidAngle::ConstPtr& msg) {
        if (msg->eye_side == "left" || msg->eye_side == "both") {
            updateSingleEyeParam("left", "lid_angle", -msg->lid_angle, msg->duration);
        }

        if (msg->eye_side == "right" || msg->eye_side == "both") {
            updateSingleEyeParam("right", "lid_angle", msg->lid_angle, msg->duration);
        }
    }

    void colorCallback(const logos_msgs::EyeColor::ConstPtr& msg) {
        updateTargetParam(
            msg->eye_side,
            "color",
            0.0,
            msg->duration,
            normalizeHexColor(msg->color)
        );
    }

    void audioWaveCallback(const logos_msgs::AudioWave::ConstPtr& msg) {
        audio_wave_.resize(msg->data.size());

        for (size_t i = 0; i < msg->data.size(); ++i) {
            audio_wave_[i] = static_cast<float>(msg->data[i]) / 32767.0f;
        }

        audio_sample_rate_ = msg->sample_rate;
        audio_start_time_ = ros::Time::now();

        if (audio_sample_rate_ > 0.0) {
            audio_duration_ = static_cast<double>(audio_wave_.size()) / audio_sample_rate_;
        } else {
            audio_duration_ = 0.0;
        }
    }

    void hudEventCallback(const std_msgs::String::ConstPtr& msg) {
        {
            std::lock_guard<std::mutex> lock(hud_event_mutex_);
            hud_event_queue_.push_back(msg->data);
        }
        hud_event_cv_.notify_one();
    }

    void hudEventWorker() {
        while (ros::ok() && !quit_requested_) {
            std::string payload;
            {
                std::unique_lock<std::mutex> lock(hud_event_mutex_);
                hud_event_cv_.wait(lock, [this]() {
                    return hud_event_stop_ || !hud_event_queue_.empty();
                });

                if (hud_event_stop_ && hud_event_queue_.empty()) {
                    return;
                }

                payload = hud_event_queue_.front();
                hud_event_queue_.pop_front();
            }

            payload = prepareHudEventPayload(payload);

            std::lock_guard<std::mutex> lock(param_mutex_);
            applyHudEventLocked(payload);
        }
    }

    int currentFigletWidthLocked() const {
        int width = terminal_cols_;
        if (caca_canvas_) {
            width = std::max(1, caca_get_canvas_width(caca_canvas_));
        }
        return std::max(8, width);
    }

    std::string prepareHudEventPayload(const std::string& payload) {
        if (payload.empty()) {
            return payload;
        }

        try {
            std::stringstream ss(payload);
            boost::property_tree::ptree root;
            boost::property_tree::read_json(ss, root);

            if (root.get<bool>("figlet_rendered", false)) {
                return payload;
            }

            const std::string pane = toLower(root.get<std::string>("pane", "face"));
            const std::string kind = toLower(root.get<std::string>("kind", "text"));
            const bool needs_figlet =
                (pane == "face" && kind == "figlet") ||
                (pane != "face" && (kind == "figlet" || kind == "caption"));

            if (!needs_figlet) {
                return payload;
            }

            const std::string text = root.get<std::string>("text", "");
            if (text.empty()) {
                return payload;
            }

            int width = 80;
            std::string default_font;
            {
                std::lock_guard<std::mutex> lock(param_mutex_);
                width = currentFigletWidthLocked();
                default_font = (kind == "caption") ? caption_figlet_font_ : default_figlet_font_;
            }

            const std::string font = root.get<std::string>("font", default_font);
            root.put("text", renderFiglet(text, font, pane, width));
            root.put("figlet_rendered", true);

            std::ostringstream out;
            boost::property_tree::write_json(out, root, false);
            return out.str();
        } catch (const std::exception&) {
            return payload;
        }
    }

    void layer0ImageCallback(const sensor_msgs::Image::ConstPtr& msg) {
        layerImageCallback(msg, 0);
    }

    void layer2ImageCallback(const sensor_msgs::Image::ConstPtr& msg) {
        layerImageCallback(msg, 2);
    }

    void layerImageCallback(const sensor_msgs::Image::ConstPtr& msg, int layer) {
        cv_bridge::CvImageConstPtr cv_ptr;
        try {
            cv_ptr = cv_bridge::toCvShare(msg, sensor_msgs::image_encodings::BGR8);
        } catch (const cv_bridge::Exception&) {
            try {
                cv_ptr = cv_bridge::toCvCopy(msg, sensor_msgs::image_encodings::BGR8);
            } catch (const cv_bridge::Exception& e) {
                ROS_WARN_STREAM("Failed to convert face layer " << layer << " image: " << e.what());
                return;
            }
        }

        if (!cv_ptr || cv_ptr->image.empty()) {
            return;
        }

        std::lock_guard<std::mutex> lock(param_mutex_);
        const ros::Time now = ros::Time::now();
        LayerImageState& image = layerImageStateLocked(layer);
        const bool replace_active_image = layerImageActiveLocked(image, now);

        image.image_bgr = cv_ptr->image.clone();
        image.resized_bgr.release();
        image.active = true;

        if (replace_active_image) {
            image.start_time = now - ros::Duration(layer_image_fade_in_sec_);
        } else {
            image.start_time = now;
        }
    }

    void applyHudEventLocked(const std::string& payload) {
        if (payload.empty()) {
            return;
        }

        try {
            std::stringstream ss(payload);
            boost::property_tree::ptree root;
            boost::property_tree::read_json(ss, root);

            const std::string pane = toLower(root.get<std::string>("pane", "face"));
            const std::string kind = toLower(root.get<std::string>("kind", "text"));
            const bool figlet_rendered = root.get<bool>("figlet_rendered", false);

            if (!isHudPane(pane)) {
                ROS_WARN_STREAM("Ignoring HUD event for unknown pane '" << pane << "'.");
                return;
            }

            if (pane == "all" && kind != "clear" && !root.get<bool>("clear", false)) {
                ROS_WARN_STREAM("Ignoring non-clear HUD event for pane 'all'.");
                return;
            }

            const boost::optional<int> clear_layer = root.get_optional<int>("layer");
            if (kind == "clear" || root.get<bool>("clear", false)) {
                clearHudPaneLocked(pane, clear_layer);
                if (kind == "clear") {
                    return;
                }
            }

            const std::string text = root.get<std::string>("text", "");
            if (text.empty()) {
                return;
            }

            if (kind == "caption" && pane != "status") {
                ROS_WARN_STREAM("Ignoring caption HUD event for pane '" << pane << "'. Captions target status.");
                return;
            }

            if (pane == "face" && kind != "text" && kind != "figlet") {
                ROS_WARN_STREAM("Ignoring unsupported face HUD kind '" << kind << "'.");
                return;
            }

            uint8_t fg = status_default_color_;
            if (kind == "caption") {
                fg = caption_default_color_;
            } else if (pane == "face") {
                fg = face_default_color_;
            }

            const boost::optional<std::string> color = root.get_optional<std::string>("color");
            if (color) {
                fg = colorNameToCaca(*color);
            }
            const uint8_t bg = colorNameToCaca(root.get<std::string>("bg_color", "black"));

            if (pane == "face") {
                const int layer = root.get<int>("layer", 0);
                if (!isFaceLayer(layer)) {
                    ROS_WARN_STREAM("Ignoring face HUD event for invalid layer '" << layer << "'. Use 0 or 2.");
                    return;
                }

                const std::string rendered = kind == "figlet"
                    ? (figlet_rendered
                        ? text
                        : renderFigletLocked(text, root.get<std::string>("font", default_figlet_font_), pane))
                    : text;
                const std::string effect = toLower(root.get<std::string>("effect", "terminal"));
                applyFaceTextEffectLocked(
                    layer,
                    effect,
                    rendered,
                    fg,
                    bg,
                    root.get<double>("duration", 0.0),
                    root.get<double>("speed", 8.0),
                    root.get<double>("density", 0.18)
                );
                return;
            }

            if (kind == "figlet" || kind == "caption") {
                const std::string font = root.get<std::string>(
                    "font",
                    kind == "caption" ? caption_figlet_font_ : default_figlet_font_
                );
                const std::string rendered = figlet_rendered
                    ? text
                    : renderFigletLocked(text, font, pane);
                if (kind == "caption") {
                    enqueueStatusPrintLocked(rendered, fg, bg, root.get<double>("duration", 0.0), true);
                } else if (pane == "status") {
                    enqueueStatusPrintLocked(rendered, fg, bg, 0.0, false);
                }
                return;
            }

            if (pane == "status") {
                enqueueStatusPrintLocked(text, fg, bg, 0.0, false);
            }
        } catch (const std::exception& e) {
            ROS_WARN_STREAM("Failed to parse HUD event JSON: " << e.what());
        }
    }

    static bool isHudPane(const std::string& pane) {
        return pane == "face" || pane == "status" || pane == "all";
    }

    static bool isFaceLayer(int layer) {
        return layer == 0 || layer == 2;
    }

    static int faceLayerIndex(int layer) {
        return layer == 2 ? 1 : 0;
    }

    void clearFaceLayerLocked(int layer) {
        FaceLayerState& state = face_layers_[faceLayerIndex(layer)];
        state.terminal_lines.clear();
        state.crawl.active = false;
        state.crawl.lines.clear();
        state.rain.active = false;
        state.rain.chars.clear();

        LayerImageState& image = layer_images_[faceLayerIndex(layer)];
        image.active = false;
        image.image_bgr.release();
        image.resized_bgr.release();
    }

    void clearFaceLayersLocked() {
        clearFaceLayerLocked(0);
        clearFaceLayerLocked(2);
    }

    void clearHudPaneLocked(const std::string& pane, const boost::optional<int>& layer) {
        if (pane == "status") {
            status_lines_.clear();
            status_print_jobs_.clear();
        } else if (pane == "all") {
            clearFaceLayersLocked();
            status_lines_.clear();
            status_print_jobs_.clear();
        } else if (layer) {
            if (isFaceLayer(*layer)) {
                clearFaceLayerLocked(*layer);
            } else {
                ROS_WARN_STREAM("Ignoring clear for invalid face HUD layer '" << *layer << "'.");
            }
        } else {
            clearFaceLayersLocked();
        }
    }

    void applyFaceTextEffectLocked(
        int layer,
        const std::string& effect,
        const std::string& text,
        uint8_t fg,
        uint8_t bg,
        double duration,
        double speed,
        double density
    ) {
        FaceLayerState& state = face_layers_[faceLayerIndex(layer)];
        const ros::Time expires_at = duration > 0.0
            ? ros::Time::now() + ros::Duration(duration)
            : ros::Time(0);

        if (effect == "crawl" || effect == "scroll" || effect == "marquee") {
            const std::vector<std::string> text_lines = splitLines(text);
            state.crawl.lines.clear();
            state.crawl.lines.reserve(text_lines.size());
            for (const std::string& line : text_lines) {
                state.crawl.lines.push_back({line, fg, bg, expires_at});
            }
            state.crawl.start_time = ros::Time::now();
            state.crawl.speed = std::max(0.1, speed);
            state.crawl.duration = std::max(0.0, duration);
            state.crawl.active = !state.crawl.lines.empty();
            return;
        }

        if (effect == "rain" || effect == "matrix") {
            state.rain.chars.clear();
            for (const char ch : text) {
                const unsigned char uch = static_cast<unsigned char>(ch);
                if (uch >= 0x21 && uch < 0x7f) {
                    state.rain.chars.push_back(ch);
                }
            }
            if (state.rain.chars.empty()) {
                state.rain.chars = "LOGOS";
            }
            state.rain.start_time = ros::Time::now();
            state.rain.speed = std::max(0.1, speed);
            state.rain.duration = std::max(0.0, duration);
            state.rain.density = clampDouble(density, 0.01, 1.0);
            state.rain.fg = fg;
            state.rain.bg = bg;
            state.rain.active = true;
            return;
        }

        if (effect != "terminal" && effect != "term" && effect != "print") {
            ROS_WARN_STREAM("Unknown face HUD effect '" << effect << "'; using terminal.");
        }

        const std::vector<std::string> lines = splitLines(text);
        for (const std::string& line : lines) {
            state.terminal_lines.push_back({line, fg, bg, expires_at});
        }

        trimFaceTerminalLinesLocked(state);
    }

    void trimFaceTerminalLinesLocked(FaceLayerState& state) {
        const ros::Time now = ros::Time::now();
        while (!state.terminal_lines.empty() &&
               state.terminal_lines.front().expires_at != ros::Time(0) &&
               state.terminal_lines.front().expires_at <= now) {
            state.terminal_lines.pop_front();
        }

        while (static_cast<int>(state.terminal_lines.size()) > face_max_lines_) {
            state.terminal_lines.pop_front();
        }
    }

    void enqueueStatusPrintLocked(
        const std::string& rendered,
        uint8_t fg,
        uint8_t bg,
        double duration,
        bool caption
    ) {
        const std::vector<std::string> text_lines = splitLines(rendered);
        std::vector<HudLine> lines;
        lines.reserve(text_lines.size());

        for (const std::string& line : text_lines) {
            lines.push_back({line, fg, bg, ros::Time(0)});
        }

        if (lines.empty()) {
            return;
        }

        StatusPrintJob job;
        job.lines = lines;
        job.start_time = ros::Time(0);
        job.duration = caption ? std::max(0.0, duration) : 0.0;
        job.next_line_index = 0;
        job.caption = caption && duration > 0.0 && lines.size() > 1;
        job.started = false;

        if (!job.caption && !status_print_jobs_.empty() && status_print_jobs_.front().caption) {
            std::deque<StatusPrintJob>::iterator insert_at = status_print_jobs_.begin();
            ++insert_at;
            while (insert_at != status_print_jobs_.end() && !insert_at->caption) {
                ++insert_at;
            }
            status_print_jobs_.insert(insert_at, job);
        } else {
            status_print_jobs_.push_back(job);
        }
    }

    void trimStatusLinesLocked() {
        while (static_cast<int>(status_lines_.size()) > status_max_lines_) {
            status_lines_.pop_front();
        }
    }

    void updateStatusPrintJobsLocked(const ros::Time& now) {
        while (!status_print_jobs_.empty()) {
            StatusPrintJob& job = status_print_jobs_.front();
            if (!job.started) {
                job.started = true;
                job.start_time = now;
            }

            size_t target_count = job.lines.size();
            if (job.caption) {
                const double elapsed = std::max(0.0, (now - job.start_time).toSec());
                const double line_duration = job.duration / static_cast<double>(job.lines.size());
                target_count = std::min(
                    job.lines.size(),
                    line_duration <= 0.0
                        ? job.lines.size()
                        : static_cast<size_t>(std::floor(elapsed / line_duration)) + 1
                );
            }

            while (job.next_line_index < target_count) {
                status_lines_.push_back(job.lines[job.next_line_index]);
                ++job.next_line_index;
                trimStatusLinesLocked();
            }

            if (job.next_line_index < job.lines.size()) {
                break;
            }

            status_print_jobs_.pop_front();
        }
    }

    static std::string shellEscape(const std::string& value) {
        std::string escaped = "'";
        for (const char c : value) {
            if (c == '\'') {
                escaped += "'\\''";
            } else {
                escaped += c;
            }
        }
        escaped += "'";
        return escaped;
    }

    static std::string sanitizeFigletFont(const std::string& font) {
        std::string sanitized;
        for (const char c : font) {
            const unsigned char ch = static_cast<unsigned char>(c);
            if (std::isalnum(ch) || c == '_' || c == '-') {
                sanitized += c;
            }
        }

        return sanitized.empty() ? "standard" : sanitized;
    }

    static bool isPlainTextFigletFont(const std::string& font) {
        const std::string value = toLower(font);
        return value == "term" ||
            value == "terminal" ||
            value == "plain" ||
            value == "text" ||
            value == "ascii" ||
            value == "none";
    }

    std::string renderFigletLocked(const std::string& text, const std::string& font, const std::string& pane) {
        return renderFiglet(text, font, pane, currentFigletWidthLocked());
    }

    std::string renderFiglet(
        const std::string& text,
        const std::string& font,
        const std::string& pane,
        int width
    ) {
        if (isPlainTextFigletFont(font)) {
            return text;
        }

        width = std::max(8, width);

        const std::string safe_font = sanitizeFigletFont(font);
        std::ostringstream command;
        command
            << "python3 -m pyfiglet -w "
            << width
            << " -f "
            << shellEscape(safe_font)
            << " -- "
            << shellEscape(text)
            << " 2>/dev/null";

        FILE* pipe = popen(command.str().c_str(), "r");
        if (!pipe) {
            return text;
        }

        std::array<char, 256> buffer;
        std::string rendered;
        while (fgets(buffer.data(), static_cast<int>(buffer.size()), pipe)) {
            rendered += buffer.data();
        }

        const int status = pclose(pipe);
        if (status != 0 || rendered.empty()) {
            ROS_WARN_STREAM("pyfiglet failed for HUD pane '" << pane << "', font '" << safe_font << "'.");
            return text;
        }

        while (!rendered.empty() && (rendered.back() == '\n' || rendered.back() == '\r')) {
            rendered.pop_back();
        }

        return rendered.empty() ? text : rendered;
    }

    void sineWaveCallback(const logos_msgs::MouthSine::ConstPtr& msg) {
        effect_start_params_ = effect_params_;
        effect_target_params_ = {
            msg->frequency,
            msg->amplitude,
            msg->phase,
            msg->phase_increment,
            normalizeHexColor(msg->color)
        };

        effect_animation_duration_ = std::max(static_cast<double>(msg->duration), 0.001);
        effect_animation_start_ = ros::Time::now();

        setupAnim(
            effect_freq_anim_,
            effect_start_params_.frequency,
            effect_target_params_.frequency,
            effect_animation_duration_
        );

        setupAnim(
            effect_amp_anim_,
            effect_start_params_.amplitude,
            effect_target_params_.amplitude,
            effect_animation_duration_
        );

        setupAnim(
            effect_phase_anim_,
            effect_start_params_.phase,
            effect_target_params_.phase,
            effect_animation_duration_
        );

        setupAnim(
            effect_pinc_anim_,
            effect_start_params_.phase_increment,
            effect_target_params_.phase_increment,
            effect_animation_duration_
        );

        setupColorAnim(
            effect_color_anim_,
            effect_start_params_.color,
            effect_target_params_.color,
            effect_animation_duration_
        );
    }

    void setupAnim(AnimParam& anim, double start, double end, double duration) {
        anim.start_value = start;
        anim.end_value = end;
        anim.duration = std::max(duration, 0.001);
        anim.start_time = ros::Time::now();
        anim.active = true;
    }

    void setupColorAnim(
        ColorAnimParam& c_anim,
        const std::string& start_hex,
        const std::string& end_hex,
        double duration
    ) {
        c_anim.start_rgb = hexToRGB(start_hex);
        c_anim.end_rgb = hexToRGB(end_hex);
        c_anim.duration = std::max(duration, 0.001);
        c_anim.start_time = ros::Time::now();
        c_anim.active = true;
    }

    void updateTargetParam(
        const std::string& eye_side,
        const std::string& param,
        double value,
        double duration,
        const std::string& color_val = ""
    ) {
        if (eye_side == "both") {
            updateSingleEyeParam("left", param, value, duration, color_val);
            updateSingleEyeParam("right", param, value, duration, color_val);
        } else {
            updateSingleEyeParam(eye_side, param, value, duration, color_val);
        }
    }

    void updateSingleEyeParam(
        const std::string& eye_side,
        const std::string& param,
        double value,
        double duration,
        const std::string& color_val = ""
    ) {
        EyeParams& current = (eye_side == "left") ? current_left_ : current_right_;
        EyeParams& start = (eye_side == "left") ? start_left_ : start_right_;
        EyeParams& target = (eye_side == "left") ? target_left_ : target_right_;

        start = current;

        if (param == "color" && !color_val.empty()) {
            if (eye_side == "left") {
                setupColorAnim(left_color_anim_, start.color, color_val, duration);
            } else {
                setupColorAnim(right_color_anim_, start.color, color_val, duration);
            }
            target.color = color_val;
            return;
        }

        if (param == "gaze_x") {
            target.gaze_x = value;
            if (eye_side == "left") {
                setupAnim(left_gaze_x_anim_, start.gaze_x, target.gaze_x, duration);
            } else {
                setupAnim(right_gaze_x_anim_, start.gaze_x, target.gaze_x, duration);
            }
        } else if (param == "gaze_y") {
            target.gaze_y = value;
            if (eye_side == "left") {
                setupAnim(left_gaze_y_anim_, start.gaze_y, target.gaze_y, duration);
            } else {
                setupAnim(right_gaze_y_anim_, start.gaze_y, target.gaze_y, duration);
            }
        } else if (param == "scale_x") {
            target.scale_x = value;
            if (eye_side == "left") {
                setupAnim(left_scale_x_anim_, start.scale_x, target.scale_x, duration);
            } else {
                setupAnim(right_scale_x_anim_, start.scale_x, target.scale_x, duration);
            }
        } else if (param == "scale_y") {
            target.scale_y = value;
            if (eye_side == "left") {
                setupAnim(left_scale_y_anim_, start.scale_y, target.scale_y, duration);
            } else {
                setupAnim(right_scale_y_anim_, start.scale_y, target.scale_y, duration);
            }
        } else if (param == "lid_height") {
            target.lid_height = value;
            if (eye_side == "left") {
                setupAnim(left_lid_height_anim_, start.lid_height, target.lid_height, duration);
            } else {
                setupAnim(right_lid_height_anim_, start.lid_height, target.lid_height, duration);
            }
        } else if (param == "lid_angle") {
            target.lid_angle = value;
            if (eye_side == "left") {
                setupAnim(left_lid_angle_anim_, start.lid_angle, target.lid_angle, duration);
            } else {
                setupAnim(right_lid_angle_anim_, start.lid_angle, target.lid_angle, duration);
            }
        }
    }

    void updateAnimation() {
        const ros::Time now = ros::Time::now();

        interpolateEye(now, current_left_, target_left_, true);
        interpolateEye(now, current_right_, target_right_, false);
        interpolateEffect(now);
    }

    void interpolateEye(
        const ros::Time& now,
        EyeParams& current,
        const EyeParams& target,
        bool is_left
    ) {
        interpolateParam(
            now,
            current.gaze_x,
            is_left ? left_gaze_x_anim_ : right_gaze_x_anim_,
            target.gaze_x
        );

        interpolateParam(
            now,
            current.gaze_y,
            is_left ? left_gaze_y_anim_ : right_gaze_y_anim_,
            target.gaze_y
        );

        interpolateParam(
            now,
            current.scale_x,
            is_left ? left_scale_x_anim_ : right_scale_x_anim_,
            target.scale_x
        );

        interpolateParam(
            now,
            current.scale_y,
            is_left ? left_scale_y_anim_ : right_scale_y_anim_,
            target.scale_y
        );

        interpolateParam(
            now,
            current.lid_height,
            is_left ? left_lid_height_anim_ : right_lid_height_anim_,
            target.lid_height
        );

        interpolateParam(
            now,
            current.lid_angle,
            is_left ? left_lid_angle_anim_ : right_lid_angle_anim_,
            target.lid_angle
        );

        interpolateColorParam(
            now,
            current.color,
            is_left ? left_color_anim_ : right_color_anim_,
            target.color
        );
    }

    void interpolateParam(
        const ros::Time& now,
        double& current_val,
        AnimParam& anim,
        double end_val
    ) {
        if (!anim.active) {
            current_val = end_val;
            return;
        }

        double t = (now - anim.start_time).toSec() / anim.duration;
        if (t >= 1.0) {
            t = 1.0;
            anim.active = false;
        }

        current_val = anim.start_value + (anim.end_value - anim.start_value) * t;
    }

    void interpolateColorParam(
        const ros::Time& now,
        std::string& current_color,
        ColorAnimParam& c_anim,
        const std::string& end_hex
    ) {
        if (!c_anim.active) {
            current_color = end_hex;
            return;
        }

        double t = (now - c_anim.start_time).toSec() / c_anim.duration;
        if (t >= 1.0) {
            t = 1.0;
            c_anim.active = false;
        }

        cv::Vec3b rgb;
        for (int i = 0; i < 3; ++i) {
            rgb[i] = static_cast<uchar>(
                c_anim.start_rgb[i] +
                (c_anim.end_rgb[i] - c_anim.start_rgb[i]) * t
            );
        }

        char buf[8];
        std::snprintf(buf, sizeof(buf), "#%02X%02X%02X", rgb[2], rgb[1], rgb[0]);
        current_color = std::string(buf);
    }

    void interpolateEffect(const ros::Time& now) {
        if (effect_animation_duration_ <= 0.0) {
            return;
        }

        interpolateEffectParam(now, effect_params_.frequency, effect_freq_anim_);
        interpolateEffectParam(now, effect_params_.amplitude, effect_amp_anim_);
        interpolateEffectParam(now, effect_params_.phase, effect_phase_anim_);
        interpolateEffectParam(now, effect_params_.phase_increment, effect_pinc_anim_);

        if (effect_color_anim_.active) {
            double t = (now - effect_color_anim_.start_time).toSec() / effect_color_anim_.duration;
            if (t >= 1.0) {
                t = 1.0;
                effect_color_anim_.active = false;
            }

            cv::Vec3b rgb;
            for (int i = 0; i < 3; ++i) {
                rgb[i] = static_cast<uchar>(
                    effect_color_anim_.start_rgb[i] +
                    (effect_color_anim_.end_rgb[i] - effect_color_anim_.start_rgb[i]) * t
                );
            }

            char buf[8];
            std::snprintf(buf, sizeof(buf), "#%02X%02X%02X", rgb[2], rgb[1], rgb[0]);
            effect_params_.color = std::string(buf);
        }
    }

    void interpolateEffectParam(const ros::Time& now, double& current_val, AnimParam& anim) {
        if (!anim.active) {
            return;
        }

        double t = (now - anim.start_time).toSec() / anim.duration;
        if (t >= 1.0) {
            t = 1.0;
            anim.active = false;
        }

        current_val = anim.start_value + (anim.end_value - anim.start_value) * t;
    }

    void renderCallback(const ros::TimerEvent&) {
        std::lock_guard<std::mutex> lock(param_mutex_);

        if (quit_requested_) {
            ros::shutdown();
            return;
        }

        if (using_caca_display_) {
            pollCacaEventsLocked();
        }

        ensureRenderGeometryLocked();
        updateAnimation();

        frame_bgr_.setTo(cv::Scalar(0, 0, 0));

        renderLayerImageOverlayLocked(frame_bgr_, 0);
        renderEyes(frame_bgr_);
        renderWaveform(frame_bgr_);
        renderLayerImageOverlayLocked(frame_bgr_, 2);
        ditherToCanvasLocked(frame_bgr_);

        if (using_caca_display_) {
            caca_refresh_display(caca_display_);
        } else {
            std::string ansi = exportCanvasAnsiLocked();
            ansi = "\033[H" + ansi + "\033[H";
            std::cout << ansi << std::endl;
            std::fflush(stdout);
        }

        publishLiveStateJson();
    }

    void pollCacaEventsLocked() {
        if (!caca_display_) {
            return;
        }

        caca_event_t ev;
        bool fps_adjusted_this_poll = false;

        while (caca_get_event(
            caca_display_,
            CACA_EVENT_KEY_PRESS | CACA_EVENT_RESIZE | CACA_EVENT_QUIT,
            &ev,
            0)) {
            const unsigned int type = caca_get_event_type(&ev);

            if (type == CACA_EVENT_QUIT) {
                quit_requested_ = true;
                continue;
            }

            if (type == CACA_EVENT_RESIZE) {
                terminal_cols_ = caca_get_event_resize_width(&ev);
                terminal_rows_ = caca_get_event_resize_height(&ev);
                ensureRenderGeometryLocked();
                continue;
            }

            if (type == CACA_EVENT_KEY_PRESS) {
                const int ch = caca_get_event_key_ch(&ev);
                const char key = static_cast<char>(ch);

                if ((key == KEY_INCREASE_FPS || key == KEY_DECREASE_FPS) &&
                    fps_adjusted_this_poll) {
                    continue;
                }

                handleKeyPress(key);

                if (key == KEY_INCREASE_FPS || key == KEY_DECREASE_FPS) {
                    fps_adjusted_this_poll = true;
                }
            }
        }
    }

    void ditherToCanvasLocked(const cv::Mat& img) {
        if (!caca_canvas_ || !caca_dither_) {
            return;
        }

        updateStatusPrintJobsLocked(ros::Time::now());

        cv::cvtColor(img, rgba_, cv::COLOR_BGR2RGBA);

        // Make pure black transparent so libcaca treats it like empty space.
        cv::inRange(img, cv::Scalar(0, 0, 0), cv::Scalar(0, 0, 0), black_mask_);
        // Maybe in the future:
        // cv::inRange(img, cv::Scalar(0, 0, 0), cv::Scalar(8, 8, 8), black_mask_);
        cv::bitwise_not(black_mask_, alpha_);

        std::vector<cv::Mat> channels;
        cv::split(rgba_, channels);
        channels[3] = alpha_;
        cv::merge(channels, rgba_);

        caca_clear_canvas(caca_canvas_);

        int w = caca_get_canvas_width(caca_canvas_);
        int h = caca_get_canvas_height(caca_canvas_);

        if (w < 1) {
            w = 1;
        }
        if (h < 1) {
            h = 1;
        }

        const int status_h = statusPaneHeightForCanvas(h);
        const int face_h = facePaneHeightForCanvas(h);

        drawFaceLayerLocked(0, 0, 0, w, face_h);
        caca_dither_bitmap(caca_canvas_, 0, 0, w, face_h, caca_dither_, rgba_.data);
        drawFaceLayerLocked(2, 0, 0, w, face_h);
        drawHudLinesLocked(status_lines_, 0, std::max(0, h - status_h), w, status_h, false);
    }

    LayerImageState& layerImageStateLocked(int layer) {
        return layer_images_[faceLayerIndex(layer)];
    }

    bool layerImageActiveLocked(const LayerImageState& image, const ros::Time& now) const {
        if (!image.active || image.image_bgr.empty()) {
            return false;
        }

        const double total = layer_image_fade_in_sec_ + layer_image_hold_sec_ + layer_image_fade_out_sec_;
        return (now - image.start_time).toSec() <= total;
    }

    double layerImageAlphaLocked(LayerImageState& image, const ros::Time& now) {
        if (!layerImageActiveLocked(image, now)) {
            image.active = false;
            image.image_bgr.release();
            image.resized_bgr.release();
            return 0.0;
        }

        const double elapsed = std::max(0.0, (now - image.start_time).toSec());

        if (layer_image_fade_in_sec_ > 0.0 && elapsed < layer_image_fade_in_sec_) {
            return layer_image_max_alpha_ * clampDouble(elapsed / layer_image_fade_in_sec_, 0.0, 1.0);
        }

        const double fade_out_start = layer_image_fade_in_sec_ + layer_image_hold_sec_;
        if (elapsed < fade_out_start || layer_image_fade_out_sec_ <= 0.0) {
            return layer_image_max_alpha_;
        }

        const double fade_out_t = (elapsed - fade_out_start) / layer_image_fade_out_sec_;
        return layer_image_max_alpha_ * (1.0 - clampDouble(fade_out_t, 0.0, 1.0));
    }

    void renderLayerImageOverlayLocked(cv::Mat& img, int layer) {
        LayerImageState& image = layerImageStateLocked(layer);
        const double alpha = layerImageAlphaLocked(image, ros::Time::now());
        if (alpha <= 0.0 || image.image_bgr.empty()) {
            return;
        }

        if (image.resized_bgr.size() != img.size()) {
            cv::resize(image.image_bgr, image.resized_bgr, img.size(), 0.0, 0.0, cv::INTER_AREA);
        }

        cv::Mat overlay = image.resized_bgr.clone();
        cv::Mat black_pixels;
        cv::inRange(overlay, cv::Scalar(0, 0, 0), cv::Scalar(0, 0, 0), black_pixels);
        overlay.setTo(cv::Scalar(1, 1, 1), black_pixels);

        cv::addWeighted(overlay, alpha, img, 1.0 - alpha, 0.0, img);
    }

    void drawFaceLayerLocked(int layer, int x0, int y0, int w, int h) {
        if (w <= 0 || h <= 0) {
            return;
        }

        FaceLayerState& state = face_layers_[faceLayerIndex(layer)];
        trimFaceTerminalLinesLocked(state);
        drawRainEffectLocked(state, x0, y0, w, h);
        drawCrawlEffectLocked(state, x0, y0, w, h);
        drawHudLinesLocked(state.terminal_lines, x0, y0, w, h, true);
    }

    void drawCrawlEffectLocked(FaceLayerState& state, int x0, int y0, int w, int h) {
        if (!state.crawl.active || state.crawl.lines.empty() || w <= 0 || h <= 0) {
            return;
        }

        const ros::Time now = ros::Time::now();
        if (state.crawl.duration > 0.0 &&
            (now - state.crawl.start_time).toSec() > state.crawl.duration) {
            state.crawl.active = false;
            state.crawl.lines.clear();
            return;
        }

        const double elapsed = std::max(0.0, (now - state.crawl.start_time).toSec());
        const int visible_count = std::min(h, static_cast<int>(state.crawl.lines.size()));
        const int first_y = y0 + std::max(0, static_cast<int>((h - visible_count) / 1.5));

        for (int i = 0; i < visible_count; ++i) {
            const HudLine& line = state.crawl.lines[i];
            std::string tile = line.text.empty() ? " " : line.text;
            tile += "   ";
            if (tile.empty()) {
                continue;
            }

            const int offset = static_cast<int>(std::floor(elapsed * state.crawl.speed)) %
                std::max(1, static_cast<int>(tile.size()));
            caca_set_color_ansi(caca_canvas_, line.fg, line.bg);

            for (int x = 0; x < w; ++x) {
                const unsigned char ch = static_cast<unsigned char>(
                    tile[(x + offset) % tile.size()]
                );
                if (ch > 0x20 && ch < 0x7f) {
                    caca_put_char(caca_canvas_, x0 + x, first_y + i, ch);
                }
            }
        }
    }

    void drawRainEffectLocked(FaceLayerState& state, int x0, int y0, int w, int h) {
        if (!state.rain.active || state.rain.chars.empty() || w <= 0 || h <= 0) {
            return;
        }

        const ros::Time now = ros::Time::now();
        if (state.rain.duration > 0.0 &&
            (now - state.rain.start_time).toSec() > state.rain.duration) {
            state.rain.active = false;
            state.rain.chars.clear();
            return;
        }

        const double elapsed = std::max(0.0, (now - state.rain.start_time).toSec());
        const int column_count = clampInt(
            static_cast<int>(std::lround(static_cast<double>(w) * state.rain.density)),
            1,
            std::max(1, w)
        );
        const int phase = static_cast<int>(std::floor(elapsed * state.rain.speed));

        caca_set_color_ansi(caca_canvas_, state.rain.fg, state.rain.bg);
        for (int i = 0; i < column_count; ++i) {
            const int x = (i * 37 + phase / 3) % w;
            const int trail = 3 + (i % 8);
            const int cycle = h + trail + 1;
            const int head = (phase + i * 11) % cycle;

            for (int j = 0; j < trail; ++j) {
                const int y = head - j;
                if (y < 0 || y >= h) {
                    continue;
                }

                const size_t char_index = static_cast<size_t>(
                    i * 13 + j * 7 + phase
                ) % state.rain.chars.size();
                const unsigned char ch = static_cast<unsigned char>(state.rain.chars[char_index]);
                if (ch > 0x20 && ch < 0x7f) {
                    caca_put_char(caca_canvas_, x0 + x, y0 + y, ch);
                }
            }
        }
    }

    void drawHudLinesLocked(
        const std::deque<HudLine>& lines,
        int x0,
        int y0,
        int w,
        int h,
        bool transparent_spaces
    ) {
        if (lines.empty() || w <= 0 || h <= 0) {
            return;
        }

        const ros::Time now = ros::Time::now();
        const int visible_count = std::min(h, static_cast<int>(lines.size()));
        const int first_line = static_cast<int>(lines.size()) - visible_count;
        const int first_y = y0 + h - visible_count;

        for (int i = 0; i < visible_count; ++i) {
            const int y = first_y + i;
            const HudLine& line = lines[first_line + i];
            if (line.expires_at != ros::Time(0) && line.expires_at <= now) {
                continue;
            }
            caca_set_color_ansi(caca_canvas_, line.fg, line.bg);

            for (int x = 0; x < w && x < static_cast<int>(line.text.size()); ++x) {
                const unsigned char ch = static_cast<unsigned char>(line.text[x]);
                if (transparent_spaces && ch == 0x20) {
                    continue;
                }
                if (ch >= 0x20 && ch < 0x7f) {
                    caca_put_char(caca_canvas_, x0 + x, y, ch);
                }
            }
        }
    }

    std::string exportCanvasAnsiLocked() {
        if (!caca_canvas_) {
            return "";
        }

        size_t len = 0;
        void* exported = caca_export_canvas_to_memory(caca_canvas_, "ansi", &len);
        std::string output;

        if (exported && len > 0) {
            output.assign(static_cast<char*>(exported), len);
            std::free(exported);
        }

        return output;
    }

    void renderEyes(cv::Mat& img) {
        const int half_w = img.cols / 2;
        renderEye(img, 0, half_w, current_left_);
        renderEye(img, half_w, img.cols, current_right_);
    }

    void renderEye(cv::Mat& img, int x_start, int x_end, const EyeParams& eye) {
        const double region_width = static_cast<double>(x_end - x_start);
        const double center_x_base = x_start + (region_width * 0.5);
        const double center_y_base = img.rows * eye_center_y_ratio_;

        const double gaze_x = eye.gaze_x * (region_width * eye_gaze_x_ratio_);
        const double gaze_y = -eye.gaze_y * (img.rows * eye_gaze_y_ratio_);

        const double sx = std::max(0.01, eye.scale_x) * (region_width * eye_radius_x_ratio_);
        const double sy = std::max(0.01, eye.scale_y) * (img.rows * eye_radius_y_ratio_);
        const double lid_height = eye.lid_height * (img.rows * eye_lid_height_ratio_);

        const double center_x = center_x_base + gaze_x;
        const double center_y = center_y_base + gaze_y;

        const cv::Vec3b eye_rgb = hexToRGB(eye.color);
        const cv::Scalar eye_color(eye_rgb[0], eye_rgb[1], eye_rgb[2]);

        const cv::Vec3b waveform_rgb = hexToRGB(effect_params_.color);
        const cv::Scalar waveform_color(waveform_rgb[0], waveform_rgb[1], waveform_rgb[2]);

        cv::Rect eye_roi_rect(x_start, 0, std::max(1, x_end - x_start), img.rows);
        cv::Mat eye_background = img(eye_roi_rect).clone();

        cv::ellipse(
            img,
            cv::Point(static_cast<int>(center_x), static_cast<int>(center_y)),
            cv::Size(static_cast<int>(sx), static_cast<int>(sy)),
            0.0,
            0.0,
            360.0,
            eye_color,
            -1
        );

        cv::ellipse(
            img,
            cv::Point(static_cast<int>(center_x), static_cast<int>(center_y)),
            cv::Size(static_cast<int>(sx), static_cast<int>(sy)),
            0.0,
            0.0,
            360.0,
            waveform_color,
            eye_outline_thickness_px_
        );

        const double lid_angle_rad = eye.lid_angle * M_PI / 180.0;
        const double lid_scale = std::max((sy + sx + 10.0) / 2.0, sx);

        const double lid_x1 = center_x - std::max(lid_scale, 10.0);
        const double lid_x2 = center_x + std::max(lid_scale, 10.0);
        const double lid_y1 = center_y
            - std::max(lid_scale, 10.0) * std::sin(lid_angle_rad)
            - lid_height * std::cos(lid_angle_rad);
        const double lid_y2 = center_y
            + std::max(lid_scale, 10.0) * std::sin(lid_angle_rad)
            - lid_height * std::cos(lid_angle_rad);

        const cv::Scalar lid_color(
            (waveform_color[0] + eye_color[0]) * 0.5,
            (waveform_color[1] + eye_color[1]) * 0.5,
            (waveform_color[2] + eye_color[2]) * 0.5
        );

        const int erase_padding = std::max(
            2,
            static_cast<int>(std::lround(img.cols * eye_lid_erase_padding_x_ratio_))
        );
        int erase_lid_x1 = static_cast<int>(lid_x1) - erase_padding;
        int erase_lid_x2 = static_cast<int>(lid_x2) + erase_padding;

        erase_lid_x1 = std::max(erase_lid_x1, x_start);
        erase_lid_x2 = std::min(erase_lid_x2, x_end - 1);

        std::vector<cv::Point> poly = {
            cv::Point(erase_lid_x1 - x_start, static_cast<int>(lid_y1)),
            cv::Point(erase_lid_x2 - x_start, static_cast<int>(lid_y2)),
            cv::Point(erase_lid_x2 - x_start, 0),
            cv::Point(erase_lid_x1 - x_start, 0)
        };

        cv::Mat lid_restore_mask(eye_roi_rect.height, eye_roi_rect.width, CV_8UC1, cv::Scalar(0));
        cv::fillConvexPoly(lid_restore_mask, poly, cv::Scalar(255));
        eye_background.copyTo(img(eye_roi_rect), lid_restore_mask);

        cv::line(
            img,
            cv::Point(static_cast<int>(lid_x1), static_cast<int>(lid_y1)),
            cv::Point(static_cast<int>(lid_x2), static_cast<int>(lid_y2)),
            lid_color,
            std::max(
                eye_lid_min_thickness_px_,
                static_cast<int>(std::lround(img.rows * eye_lid_thickness_ratio_))
            )
        );
    }

    void ensureWaveBuffers(int length) {
        if (static_cast<int>(sine_wave_buffer_.size()) != length) {
            sine_wave_buffer_.assign(length, 0.0f);
            audio_buffer_.assign(length, 0.0f);
            combined_wave_.assign(length, 0.0f);
        } else {
            std::fill(sine_wave_buffer_.begin(), sine_wave_buffer_.end(), 0.0f);
            std::fill(audio_buffer_.begin(), audio_buffer_.end(), 0.0f);
            std::fill(combined_wave_.begin(), combined_wave_.end(), 0.0f);
        }
    }

    void renderWaveform(cv::Mat& img) {
        const int length = img.cols;
        const int baseline = static_cast<int>(img.rows * waveform_baseline_y_ratio_);

        ensureWaveBuffers(length);
        generateSineWaveInPlace(sine_wave_buffer_);
        normalizeWave(sine_wave_buffer_);

        const double audio_elapsed = audio_wave_.empty()
            ? 0.0
            : (ros::Time::now() - audio_start_time_).toSec();

        const bool has_audio =
            !audio_wave_.empty() &&
            audio_sample_rate_ > 0.0 &&
            audio_elapsed <= audio_duration_;

        if (!has_audio && !audio_wave_.empty()) {
            audio_wave_.clear();
            audio_duration_ = 0.0;
        }

        if (has_audio) {
            updateAudioBuffer(audio_buffer_);

            for (int i = 0; i < length; ++i) {
                combined_wave_[i] = audio_buffer_[i];
            }

            normalizeWaveNoEffect(combined_wave_);

            bool first_combined_point = true;
            int prev_y_combined = 0;

            for (int i = 0; i < length; ++i) {
                const int x = i;
                const int y = static_cast<int>(
                    baseline + (combined_wave_[i] * static_cast<float>(img.rows) *
                        static_cast<float>(waveform_amplitude_y_ratio_))
                );

                if (!first_combined_point) {
                    const double max_wave_height = static_cast<double>(img.rows) *
                        waveform_amplitude_y_ratio_;
                    const int color_y = (std::abs(y - baseline) > std::abs(prev_y_combined - baseline))
                        ? y
                        : prev_y_combined;

                    const cv::Scalar color = getColorFromVerticalDistance(
                        color_y,
                        baseline,
                        max_wave_height
                    );

                    cv::line(
                        img,
                        cv::Point(x - 1, prev_y_combined),
                        cv::Point(x, y),
                        color,
                        std::max(
                            1,
                            static_cast<int>(std::lround(img.rows * audio_wave_thickness_ratio_))
                        )
                    );
                }

                prev_y_combined = y;
                first_combined_point = false;
            }
        }

        bool first_sine_point = true;
        int prev_y_sine = 0;

        const cv::Vec3b sine_rgb = hexToRGB(effect_params_.color);
        const cv::Scalar sine_color(sine_rgb[0], sine_rgb[1], sine_rgb[2]);

        for (int i = 0; i < length; ++i) {
            const int x = i;
            const int y = static_cast<int>(
                baseline + (sine_wave_buffer_[i] * static_cast<float>(img.rows) *
                    static_cast<float>(waveform_amplitude_y_ratio_))
            );

            if (!first_sine_point) {
                cv::line(
                    img,
                    cv::Point(x - 1, prev_y_sine),
                    cv::Point(x, y),
                    sine_color,
                    mouth_sine_thickness_
                );
            }

            prev_y_sine = y;
            first_sine_point = false;
        }
    }

    void updateAudioBuffer(std::vector<float>& buffer) {
        if (audio_wave_.empty() || audio_sample_rate_ <= 0.0) {
            return;
        }

        const double elapsed = (ros::Time::now() - audio_start_time_).toSec();
        if (elapsed > audio_duration_) {
            return;
        }

        const int needed = static_cast<int>(buffer.size());
        const int samples_passed = static_cast<int>(elapsed * audio_sample_rate_);
        const int start = std::max(0, samples_passed - needed);
        const int end = std::min(static_cast<int>(audio_wave_.size()), start + needed);
        const int len = end - start;

        if (len > 0) {
            std::copy(audio_wave_.begin() + start, audio_wave_.begin() + end, buffer.begin());
        }
    }

    void generateSineWaveInPlace(std::vector<float>& wave) {
        const int num_samples = static_cast<int>(wave.size());
        if (num_samples <= 0) {
            return;
        }

        for (int i = 0; i < num_samples; ++i) {
            const double t =
                static_cast<double>(i) / static_cast<double>(num_samples) * 2.0 * M_PI;

            wave[i] = static_cast<float>(
                effect_params_.amplitude *
                std::sin(effect_params_.frequency * t + effect_params_.phase)
            );
        }

        effect_params_.phase += effect_params_.phase_increment;
    }

    void normalizeWave(std::vector<float>& wave) {
        if (wave.empty()) {
            return;
        }

        float minv = wave[0];
        float maxv = wave[0];

        for (const float v : wave) {
            minv = std::min(minv, v);
            maxv = std::max(maxv, v);
        }

        if (std::abs(maxv - minv) < 1e-9f) {
            std::fill(wave.begin(), wave.end(), 0.0f);
            return;
        }

        for (float& v : wave) {
            v = (2.0f * (v - minv) / (maxv - minv) - 1.0f) *
                static_cast<float>(effect_params_.amplitude);
        }
    }

    void normalizeWaveNoEffect(std::vector<float>& wave) {
        if (wave.empty()) {
            return;
        }

        float minv = wave[0];
        float maxv = wave[0];

        for (const float v : wave) {
            minv = std::min(minv, v);
            maxv = std::max(maxv, v);
        }

        if (std::abs(maxv - minv) < 1e-9f) {
            std::fill(wave.begin(), wave.end(), 0.0f);
            return;
        }

        for (float& v : wave) {
            v = (2.0f * (v - minv) / (maxv - minv) - 1.0f); // *
                //static_cast<float>(effect_params_.amplitude);
        }
    }

    cv::Vec3b hexToRGB(const std::string& hex) const {
        int r = 0;
        int g = 0;
        int b = 0;

        const std::string normalized = normalizeHexColor(hex);
        if (normalized.size() == 7 && normalized[0] == '#') {
            std::sscanf(normalized.c_str() + 1, "%02x%02x%02x", &r, &g, &b);
        }

        return cv::Vec3b(
            static_cast<uchar>(b),
            static_cast<uchar>(g),
            static_cast<uchar>(r)
        );
    }

    cv::Scalar getColorFromVerticalDistance(
        int y,
        int baseline,
        double max_wave_height
    ) const {
        if (max_wave_height <= 1e-6) {
            return amplitude_color_lut_[0];
        }

        const double distance_from_baseline = std::abs(static_cast<double>(y - baseline));
        const double t = clampDouble(distance_from_baseline / max_wave_height, 0.0, 1.0);

        int idx = static_cast<int>(std::lround(t * 255.0));
        idx = std::max(0, std::min(255, idx));

        return amplitude_color_lut_[idx];
    }

    std::string buildLiveStateJson() const {
        std::ostringstream oss;
        oss << std::fixed << std::setprecision(4);

        const double frame_duration = (fps_ > 0)
            ? (1.0 / static_cast<double>(fps_))
            : 0.0;

        oss << "{";
        oss << "\"timestamp\":" << ros::Time::now().toSec() << ",";

        oss << "\"left_eye\":{";
        oss << "\"gaze_x\":" << current_left_.gaze_x << ",";
        oss << "\"gaze_y\":" << current_left_.gaze_y << ",";
        oss << "\"scale_x\":" << current_left_.scale_x << ",";
        oss << "\"scale_y\":" << current_left_.scale_y << ",";
        oss << "\"lid_height\":" << current_left_.lid_height << ",";
        oss << "\"lid_angle\":" << current_left_.lid_angle << ",";
        oss << "\"color\":\"" << jsonEscape(current_left_.color) << "\"";
        oss << "},";

        oss << "\"right_eye\":{";
        oss << "\"gaze_x\":" << current_right_.gaze_x << ",";
        oss << "\"gaze_y\":" << current_right_.gaze_y << ",";
        oss << "\"scale_x\":" << current_right_.scale_x << ",";
        oss << "\"scale_y\":" << current_right_.scale_y << ",";
        oss << "\"lid_height\":" << current_right_.lid_height << ",";
        oss << "\"lid_angle\":" << current_right_.lid_angle << ",";
        oss << "\"color\":\"" << jsonEscape(current_right_.color) << "\"";
        oss << "},";

        oss << "\"mouth\":{";
        oss << "\"frequency\":" << effect_params_.frequency << ",";
        oss << "\"amplitude\":" << effect_params_.amplitude << ",";
        oss << "\"phase\":" << effect_params_.phase << ",";
        oss << "\"phase_increment\":" << effect_params_.phase_increment << ",";
        oss << "\"color\":\"" << jsonEscape(effect_params_.color) << "\"";
        oss << "},";

        oss << "\"duration\":" << frame_duration;
        oss << "}";

        return oss.str();
    }
    
    void publishLiveStateJson() {
        std_msgs::String msg;
        msg.data = buildLiveStateJson();
        pub_live_state_json_.publish(msg);
    }

    void setupTerminal() {
        tcgetattr(STDIN_FILENO, &orig_settings_);
        termios new_settings = orig_settings_;
        new_settings.c_lflag &= ~(ICANON | ECHO);
        tcsetattr(STDIN_FILENO, TCSANOW, &new_settings);
    }

    void restoreTerminal() {
        tcsetattr(STDIN_FILENO, TCSANOW, &orig_settings_);
    }

    void keypressListener() {
        fd_set set;
        struct timeval timeout;

        while (ros::ok() && !quit_requested_) {
            FD_ZERO(&set);
            FD_SET(STDIN_FILENO, &set);
            timeout.tv_sec = 0;
            timeout.tv_usec = 100000;

            const int rv = select(STDIN_FILENO + 1, &set, nullptr, nullptr, &timeout);
            if (rv > 0) {
                char c;
                if (read(STDIN_FILENO, &c, 1) > 0) {
                    std::lock_guard<std::mutex> lock(param_mutex_);
                    handleKeyPress(c);
                }
            }
        }
    }

    void handleKeyPress(char key) {
        if (key == KEY_QUIT) {
            quit_requested_ = true;
            return;
        }

        if (key == KEY_INCREASE_FPS) {
            if (fps_ < max_fps_) {
                ++fps_;
                updateRenderTimer();
            }
            return;
        }

        if (key == KEY_DECREASE_FPS) {
            if (fps_ > min_fps_) {
                --fps_;
                updateRenderTimer();
            }
            return;
        }

        if (key == KEY_INCREASE_FACE_PANE || key == KEY_INCREASE_FACE_PANE_ALT) {
            adjustFacePaneRatioLocked(PANE_RATIO_STEP);
            return;
        }

        if (key == KEY_DECREASE_FACE_PANE) {
            adjustFacePaneRatioLocked(-PANE_RATIO_STEP);
            return;
        }

        if (key == KEY_CLEAR_SCREEN) {
            if (!using_caca_display_) {
                std::printf("\033[2J\033[H");
                std::fflush(stdout);
            } else if (caca_canvas_) {
                caca_clear_canvas(caca_canvas_);
            }
            return;
        }

        if (using_caca_display_) {
            return;
        }

        if (key == KEY_RESET) {
            getTerminalSize();
            caca_set_canvas_size(caca_canvas_, terminal_cols_, std::max(1, terminal_rows_ - 1));
            ensureRenderGeometryLocked();
            std::printf("\033[2J\033[H");
            std::fflush(stdout);
        } else if (key == KEY_INCREASE_COLS) {
            terminal_cols_ += 1;
            caca_set_canvas_size(caca_canvas_, terminal_cols_, std::max(1, terminal_rows_ - 1));
            ensureRenderGeometryLocked();
        } else if (key == KEY_DECREASE_COLS) {
            terminal_cols_ = std::max(10, terminal_cols_ - 1);
            caca_set_canvas_size(caca_canvas_, terminal_cols_, std::max(1, terminal_rows_ - 1));
            ensureRenderGeometryLocked();
        } else if (key == KEY_INCREASE_ROWS) {
            terminal_rows_ += 1;
            caca_set_canvas_size(caca_canvas_, terminal_cols_, std::max(1, terminal_rows_ - 1));
            ensureRenderGeometryLocked();
        } else if (key == KEY_DECREASE_ROWS) {
            terminal_rows_ = std::max(10, terminal_rows_ - 1);
            caca_set_canvas_size(caca_canvas_, terminal_cols_, std::max(1, terminal_rows_ - 1));
            ensureRenderGeometryLocked();
        }
    }
};

int main(int argc, char** argv) {
    ros::init(argc, argv, "logos_face_hud");
    FaceNodeCpp node;
    node.run();
    return 0;
}
