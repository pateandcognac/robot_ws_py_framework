#include <ros/ros.h>
#include <std_msgs/String.h>
#include <std_msgs/Int32MultiArray.h>

#include <logos_msgs/EyeGazeX.h>
#include <logos_msgs/EyeGazeY.h>
#include <logos_msgs/EyeScaleX.h>
#include <logos_msgs/EyeScaleY.h>
#include <logos_msgs/EyeLidHeight.h>
#include <logos_msgs/EyeLidAngle.h>
#include <logos_msgs/EyeColor.h>
#include <logos_msgs/MouthSine.h>
#include <logos_msgs/AudioWave.h>

#include <opencv2/opencv.hpp>

#include <dynamic_reconfigure/server.h>
#include <logos_face/FaceNodeConfig.h>

#include <caca.h>
#include <cmath>
#include <map>
#include <string>
#include <mutex>
#include <thread>
#include <atomic>
#include <vector>
#include <cstdio>
#include <termios.h>
#include <unistd.h>
#include <sys/select.h>
#include <sys/ioctl.h>

// Keyboard controls
// q = quit
// r = re-detect terminal size
// a/d = decrease/increase columns
// s/w = decrease/increase rows
// [/]= decrease/increase fps
// \ = clear screen
const char KEY_QUIT = 'q';
const char KEY_RESET = 'r';
const char KEY_INCREASE_COLS = 'd';
const char KEY_DECREASE_COLS = 'a';
const char KEY_INCREASE_ROWS = 'w';
const char KEY_DECREASE_ROWS = 's';
const char KEY_INCREASE_FPS = ']';
const char KEY_DECREASE_FPS = '[';
const char KEY_CLEAR_SCREEN = '\\';

struct AnimParam {
    double start_value;
    double end_value;
    double duration;
    ros::Time start_time;
    bool active;
    AnimParam(): start_value(0), end_value(0), duration(0), active(false) {}
};

struct ColorAnimParam {
    cv::Vec3b start_rgb;
    cv::Vec3b end_rgb;
    double duration;
    ros::Time start_time;
    bool active;
    ColorAnimParam(): duration(0), active(false) {}
};

class FaceNodeCpp {
public:
    FaceNodeCpp() : nh_("~"), quit_requested_(false) {
        // Default params
        // We will detect terminal size below, so initial values are placeholders
        terminal_cols_ = 24;
        terminal_rows_ = 24;
        fps_ = nh_.param<int>("fps", 8);

        MIN_FPS_ = 1;
        MAX_FPS_ = 24;

        getTerminalSize();

        initEyeParams();
        initAudioParams();
        initEffectParams();

        // Subscribers
        sub_gaze_x_ = nh_.subscribe("/face/eye_gaze_x", 10, &FaceNodeCpp::gazeXCallback, this);
        sub_gaze_y_ = nh_.subscribe("/face/eye_gaze_y", 10, &FaceNodeCpp::gazeYCallback, this);
        sub_scale_x_ = nh_.subscribe("/face/eye_scale_x", 10, &FaceNodeCpp::scaleXCallback, this);
        sub_scale_y_ = nh_.subscribe("/face/eye_scale_y", 10, &FaceNodeCpp::scaleYCallback, this);
        sub_lid_height_ = nh_.subscribe("/face/eye_lid_height", 10, &FaceNodeCpp::lidHeightCallback, this);
        sub_lid_angle_ = nh_.subscribe("/face/eye_lid_angle", 10, &FaceNodeCpp::lidAngleCallback, this);
        sub_color_ = nh_.subscribe("/face/eye_color", 10, &FaceNodeCpp::colorCallback, this);
        sub_mouth_sine_ = nh_.subscribe("/face/mouth/sine_wave", 10, &FaceNodeCpp::sineWaveCallback, this);
        sub_audio_wave_ = nh_.subscribe("/face/mouth/audio_wave", 10, &FaceNodeCpp::audioWaveCallback, this);

        // Publishers
        notification_led_pub_ = nh_.advertise<std_msgs::Int32MultiArray>("/notification/rgbled", 10);
        pub_live_gaze_x_ = nh_.advertise<logos_msgs::EyeGazeX>("/face/live_state/eye_gaze_x",10);
        pub_live_gaze_y_ = nh_.advertise<logos_msgs::EyeGazeY>("/face/live_state/eye_gaze_y", 10);
        pub_live_scale_x_ = nh_.advertise<logos_msgs::EyeScaleX>("/face/live_state/eye_scale_x", 10);
        pub_live_scale_y_ = nh_.advertise<logos_msgs::EyeScaleY>("/face/live_state/eye_scale_y", 10);
        pub_live_lid_height_ = nh_.advertise<logos_msgs::EyeLidHeight>("/face/live_state/eye_lid_height", 10);
        pub_live_lid_angle_ = nh_.advertise<logos_msgs::EyeLidAngle>("/face/live_state/eye_lid_angle", 10);
        pub_live_color_ = nh_.advertise<logos_msgs::EyeColor>("/face/live_state/eye_color", 10);
        pub_live_mouth_sine_ = nh_.advertise<logos_msgs::MouthSine>("/face/live_state/mouth_sine_wave", 10);

        // Dynamic reconfigure (only fps will be used)
        dyn_srv_.setCallback(boost::bind(&FaceNodeCpp::configCallback, this, _1, _2));

        // Timer
        updateRenderTimer();

        // Start keypress thread
        setupTerminal();
        keypress_thread_ = std::thread(&FaceNodeCpp::keypressListener, this);
    }

    ~FaceNodeCpp() {
        quit_requested_ = true;
        if(keypress_thread_.joinable())
            keypress_thread_.join();
        restoreTerminal();
    }

    void run() {
        ros::spin();
    }

private:
    ros::NodeHandle nh_;
    ros::Subscriber sub_gaze_x_, sub_gaze_y_, sub_scale_x_, sub_scale_y_;
    ros::Subscriber sub_lid_height_, sub_lid_angle_, sub_color_;
    ros::Subscriber sub_mouth_sine_, sub_audio_wave_;

    // ros::Publisher ascii_pub_;
    ros::Publisher notification_led_pub_;
    ros::Publisher pub_live_gaze_x_;

    // **Added Publishers for Other Face Components**
    ros::Publisher pub_live_gaze_y_;
    ros::Publisher pub_live_scale_x_;
    ros::Publisher pub_live_scale_y_;
    ros::Publisher pub_live_lid_height_;
    ros::Publisher pub_live_lid_angle_;
    ros::Publisher pub_live_color_;
    ros::Publisher pub_live_mouth_sine_;
    // Note: Excluding "audio_wave" as per the requirement

    dynamic_reconfigure::Server<logos_face::FaceNodeConfig> dyn_srv_;

    ros::Timer render_timer_;
    std::mutex param_mutex_;

    int terminal_cols_;
    int terminal_rows_;
    int fps_;
    int MIN_FPS_, MAX_FPS_;

    std::atomic<bool> quit_requested_;

    // Eye params with animation
    struct EyeParams {
        double gaze_x;
        double gaze_y;
        double scale_x;
        double scale_y;
        double lid_height;
        double lid_angle;
        std::string color;
    };
    EyeParams current_left_, current_right_;
    EyeParams start_left_, start_right_;
    EyeParams target_left_, target_right_;

    // Animations for each param
    std::map<std::string, AnimParam> anim_params_; 
    std::map<std::string, ColorAnimParam> color_anim_params_;

    // Audio/Effects
    std::vector<float> audio_wave_;
    double audio_sample_rate_;
    ros::Time audio_start_time_;
    double audio_duration_;
    int audio_index_;

    struct EffectParams {
        double frequency;
        double amplitude;
        double phase;
        double phase_increment;
        std::string color;
    };
    EffectParams effect_params_, effect_start_params_, effect_target_params_;
    AnimParam effect_freq_anim_, effect_amp_anim_, effect_phase_anim_, effect_pinc_anim_;
    ColorAnimParam effect_color_anim_;

    double effect_animation_duration_;
    ros::Time effect_animation_start_;

    // Keyboard handling
    termios orig_settings_;
    std::thread keypress_thread_;


    // 1. Add the color cycling helper function
    cv::Scalar getColorFromPalette(int x, int length) {
        // Calculate hue based on x position
        float hue = (static_cast<float>(x) / length) * 179.0f; // OpenCV hue range [0,179]
        
        // Create an HSV pixel
        cv::Mat hsv(1, 1, CV_8UC3, cv::Scalar(hue, 255, 255));
        cv::Mat bgr;
        
        // Convert HSV to BGR
        cv::cvtColor(hsv, bgr, cv::COLOR_HSV2BGR);
        
        // Extract the BGR color
        cv::Vec3b color = bgr.at<cv::Vec3b>(0, 0);
        return cv::Scalar(color[0], color[1], color[2]);
    }


    void initEyeParams() {
        current_left_ = {0,0,1,1,1,0,"#00ff00ff"};
        current_right_ = {0,0,1,1,0.5,0,"#00ff00ff"}; 
        target_left_ = current_left_;
        target_right_ = current_right_;
        start_left_ = current_left_;
        start_right_ = current_right_;
    }

    void initAudioParams() {
        audio_sample_rate_ = 22050.0;
        audio_index_ = 0;
        audio_duration_ = 0.0;
    }

    void initEffectParams() {
        effect_params_ = {1.0, 1.0, 0.0, 0.1, "#00FF00"};
        effect_target_params_ = effect_params_;
        effect_start_params_ = effect_params_;
        effect_animation_duration_ = 0.0;
    }

    void getTerminalSize() {
        struct winsize w;
        ioctl(STDOUT_FILENO, TIOCGWINSZ, &w);
        terminal_rows_ = w.ws_row;
        terminal_cols_ = w.ws_col;
        // Just in case we get something weird
        if(terminal_rows_ < 4) terminal_rows_ = 20;
        if(terminal_cols_ < 4) terminal_cols_ = 20;
    }

    void configCallback(logos_face::FaceNodeConfig &config, uint32_t level) {
        std::lock_guard<std::mutex> lock(param_mutex_);
        // We ignore columns and width_ratio from config now
        if(config.fps != fps_) {
            fps_ = config.fps;
            updateRenderTimer();
        }
    }

    void updateRenderTimer() {
        if(render_timer_) render_timer_.stop();
        render_timer_ = nh_.createTimer(ros::Duration(1.0/fps_), &FaceNodeCpp::renderCallback, this);
    }

    // Subscriber Callbacks
    void gazeXCallback(const logos_msgs::EyeGazeX::ConstPtr &msg) {
        updateTargetParam(msg->eye_side, "gaze_x", msg->gaze_x, msg->duration);
    }
    void gazeYCallback(const logos_msgs::EyeGazeY::ConstPtr &msg) {
        updateTargetParam(msg->eye_side, "gaze_y", msg->gaze_y, msg->duration);
    }
    void scaleXCallback(const logos_msgs::EyeScaleX::ConstPtr &msg) {
        updateTargetParam(msg->eye_side, "scale_x", msg->scale_x, msg->duration);
    }
    void scaleYCallback(const logos_msgs::EyeScaleY::ConstPtr &msg) {
        updateTargetParam(msg->eye_side, "scale_y", msg->scale_y, msg->duration);
    }
    void lidHeightCallback(const logos_msgs::EyeLidHeight::ConstPtr &msg) {
        updateTargetParam(msg->eye_side, "lid_height", msg->lid_height, msg->duration);
    }
    void lidAngleCallback(const logos_msgs::EyeLidAngle::ConstPtr &msg) {
        if (msg->eye_side == "left" || msg->eye_side == "both")
            updateSingleEyeParam("left", "lid_angle", -msg->lid_angle, msg->duration);
        if (msg->eye_side == "right" || msg->eye_side == "both")
            updateSingleEyeParam("right", "lid_angle", msg->lid_angle, msg->duration);
    }
    void colorCallback(const logos_msgs::EyeColor::ConstPtr &msg) {
        updateTargetParam(msg->eye_side, "color", 0.0, msg->duration, msg->color);
    }
    void audioWaveCallback(const logos_msgs::AudioWave::ConstPtr &msg) {
        audio_wave_.resize(msg->data.size());
        for(size_t i=0; i<msg->data.size(); i++)
            audio_wave_[i] = (float)msg->data[i]/32767.0f;
        audio_sample_rate_ = msg->sample_rate;
        audio_index_ = 0;
        audio_start_time_ = ros::Time::now();
        audio_duration_ = (double)audio_wave_.size() / audio_sample_rate_;
    }
    void sineWaveCallback(const logos_msgs::MouthSine::ConstPtr &msg) {
        effect_start_params_ = effect_params_;
        effect_target_params_ = {msg->frequency, msg->amplitude, msg->phase, msg->phase_increment, msg->color};
        effect_animation_duration_ = std::max((float)msg->duration, 0.001f);
        effect_animation_start_ = ros::Time::now();

        setupAnim(effect_freq_anim_, effect_start_params_.frequency, effect_target_params_.frequency, effect_animation_duration_);
        setupAnim(effect_amp_anim_, effect_start_params_.amplitude, effect_target_params_.amplitude, effect_animation_duration_);
        setupAnim(effect_phase_anim_, effect_start_params_.phase, effect_target_params_.phase, effect_animation_duration_);
        setupAnim(effect_pinc_anim_, effect_start_params_.phase_increment, effect_target_params_.phase_increment, effect_animation_duration_);

        setupColorAnim(effect_color_anim_, effect_start_params_.color, effect_target_params_.color, effect_animation_duration_);
    }

    // Animation Helpers
    void setupAnim(AnimParam &anim, double start, double end, double duration) {
        anim.start_value = start;
        anim.end_value = end;
        anim.duration = duration;
        anim.start_time = ros::Time::now();
        anim.active = true;
    }

    void setupColorAnim(ColorAnimParam &c_anim, const std::string &start_hex, const std::string &end_hex, double duration) {
        c_anim.start_rgb = hexToRGB(start_hex);
        c_anim.end_rgb = hexToRGB(end_hex);
        c_anim.duration = duration;
        c_anim.start_time = ros::Time::now();
        c_anim.active = true;
    }

    void updateTargetParam(const std::string &eye_side, const std::string &param, double value, double duration, const std::string &color_val="") {
        if(eye_side == "both") {
            updateSingleEyeParam("left", param, value, duration, color_val);
            updateSingleEyeParam("right", param, value, duration, color_val);
        } else {
            updateSingleEyeParam(eye_side, param, value, duration, color_val);
        }
    }

    void updateSingleEyeParam(const std::string &eye_side, const std::string &param, double value, double duration, const std::string &color_val="") {
        EyeParams &current = (eye_side=="left")?current_left_:current_right_;
        EyeParams &start = (eye_side=="left")?start_left_:start_right_;
        EyeParams &target = (eye_side=="left")?target_left_:target_right_;

        start = current;
        if(param=="color" && !color_val.empty()){
            setupColorAnim(color_anim_params_[eye_side+"_color"], start.color, color_val, duration);
            target.color = color_val;
        } else {
            double start_val=0,end_val=0;
            if(param=="gaze_x") {start_val=start.gaze_x;end_val=value;target.gaze_x=value;}
            else if(param=="gaze_y"){start_val=start.gaze_y;end_val=value;target.gaze_y=value;}
            else if(param=="scale_x"){start_val=start.scale_x;end_val=value;target.scale_x=value;}
            else if(param=="scale_y"){start_val=start.scale_y;end_val=value;target.scale_y=value;}
            else if(param=="lid_height"){start_val=start.lid_height;end_val=value;target.lid_height=value;}
            else if(param=="lid_angle"){start_val=start.lid_angle;end_val=value;target.lid_angle=value;}

            setupAnim(anim_params_[eye_side+"_"+param], start_val, end_val, duration);
        }
    }

    void updateAnimation() {
        ros::Time now = ros::Time::now();
        interpolateEyes(now, current_left_, start_left_, target_left_);
        interpolateEyes(now, current_right_, start_right_, target_right_);
        interpolateEffect(now);
    }

    void interpolateEyes(const ros::Time &now, EyeParams &current, const EyeParams &start, const EyeParams &target) {
        bool isLeft = (&current == &current_left_);
        std::string side = isLeft?"left":"right";

        interpolateParam(now, current.gaze_x, anim_params_[side+"_gaze_x"], start.gaze_x, target.gaze_x, side+"_gaze_x");
        interpolateParam(now, current.gaze_y, anim_params_[side+"_gaze_y"], start.gaze_y, target.gaze_y, side+"_gaze_y");
        interpolateParam(now, current.scale_x, anim_params_[side+"_scale_x"], start.scale_x, target.scale_x, side+"_scale_x");
        interpolateParam(now, current.scale_y, anim_params_[side+"_scale_y"], start.scale_y, target.scale_y, side+"_scale_y");
        interpolateParam(now, current.lid_height, anim_params_[side+"_lid_height"], start.lid_height, target.lid_height, side+"_lid_height");
        interpolateParam(now, current.lid_angle, anim_params_[side+"_lid_angle"], start.lid_angle, target.lid_angle, side+"_lid_angle");
        interpolateColorParam(now, current.color, color_anim_params_[side+"_color"], start.color, target.color, side+"_color");
    }

    void interpolateParam(const ros::Time &now, double &current_val, AnimParam &anim, double start_val, double end_val, const std::string &key) {
        if(!anim.active) {
            current_val = end_val;
            return;
        }
        double t = (now - anim.start_time).toSec()/anim.duration;
        if(t>=1.0) {
            t=1.0;
            anim.active=false;
        }
        current_val = start_val + (end_val - start_val)*t;
    }

    void interpolateColorParam(const ros::Time &now, std::string &current_color, ColorAnimParam &c_anim, const std::string &start_hex, const std::string &end_hex, const std::string &key) {
        if(!c_anim.active) {
            current_color = end_hex;
            return;
        }
        double t = (now - c_anim.start_time).toSec()/c_anim.duration;
        if(t>1.0) {
            t=1.0;
            c_anim.active=false;
        }
        cv::Vec3b rgb;
        for(int i=0;i<3;i++)
            rgb[i] = (uchar)(c_anim.start_rgb[i] + (c_anim.end_rgb[i]-c_anim.start_rgb[i])*t);
        char buf[16];
        snprintf(buf,16,"#%02x%02x%02x", rgb[2],rgb[1],rgb[0]);
        current_color = std::string(buf);
    }

    void interpolateEffect(const ros::Time &now) {
        if(effect_animation_duration_<=0) {
            return;
        }
        double t = (now - effect_animation_start_).toSec()/effect_animation_duration_;
        if(t>1.0) t=1.0;
        interpolateEffectParam(now, effect_params_.frequency, effect_freq_anim_);
        interpolateEffectParam(now, effect_params_.amplitude, effect_amp_anim_);
        interpolateEffectParam(now, effect_params_.phase, effect_phase_anim_);
        interpolateEffectParam(now, effect_params_.phase_increment, effect_pinc_anim_);

        // Color
        if(effect_color_anim_.active) {
            double ct=(now - effect_color_anim_.start_time).toSec()/effect_color_anim_.duration;
            if(ct>1.0){ct=1.0; effect_color_anim_.active=false;}
            cv::Vec3b rgb;
            for(int i=0;i<3;i++)
                rgb[i]=(uchar)(effect_color_anim_.start_rgb[i] + (effect_color_anim_.end_rgb[i]-effect_color_anim_.start_rgb[i])*ct);
            char buf[16];
            snprintf(buf,16,"#%02x%02x%02x", rgb[2],rgb[1],rgb[0]);
            effect_params_.color=std::string(buf);
        }
    }

    void interpolateEffectParam(const ros::Time &now, double &current_val, AnimParam &anim) {
        if(!anim.active) return;
        double t = (now - anim.start_time).toSec()/anim.duration;
        if(t>1.0){t=1.0;anim.active=false;}
        current_val = anim.start_value + (anim.end_value - anim.start_value)*t;
    }
    // Rendering
    void renderCallback(const ros::TimerEvent&) {
        std::lock_guard<std::mutex> lock(param_mutex_);

        if(quit_requested_) {
            ros::shutdown();
            return;
        }

        updateAnimation();

        cv::Mat img(200,200,CV_8UC3, cv::Scalar(0,0,0));
        renderEyes(img);
        renderWaveform(img);

        std::string ascii = imageToAscii(img, terminal_cols_, terminal_rows_-1);

        // Move cursor to top-left and print
        fflush(stdout);
        ascii = "\033[H" + ascii + "\033[H"; 
        std::cout << ascii << std::endl;


        publishLiveStates();
    }

    void renderEyes(cv::Mat &img) {
        renderEye(img,0, current_left_);
        renderEye(img,100, current_right_);
    }

    void renderEye(cv::Mat &img, int offset_x, const EyeParams &eye) {
        double gaze_x = eye.gaze_x * 25;
        double gaze_y = -eye.gaze_y * 25;
        double sx = std::max(0.01, eye.scale_x) * 40;
        double sy = std::max(0.01, eye.scale_y) * 40;

        double lid_height = eye.lid_height * 50;
        double center_x = offset_x + 50 + gaze_x;
        double center_y = 75 + gaze_y;

        cv::Vec3b c = hexToRGB(eye.color);
        cv::Scalar colScalar(c[0], c[1], c[2]);

        cv::Vec3b waveform_rgb = hexToRGB(effect_params_.color);
        cv::Scalar waveform_color_scalar(waveform_rgb[0], waveform_rgb[1], waveform_rgb[2]);

        cv::ellipse(img, cv::Point(center_x, center_y), cv::Size(sx, sy), 0, 0, 360, colScalar, -1);
        cv::ellipse(img, cv::Point(center_x, center_y), cv::Size(sx, sy), 0, 0, 360, waveform_color_scalar, 2); // rim of eye, waveform color, add knob

        double lid_angle_rad = eye.lid_angle * M_PI / 180.0;
        // I was messing with this lid scale calc
        double lid_scale = std::max((sy + sx + 10) / 2, sx);
        // double lid_scale = std::max((sy + sx) / 2, sx);
        double lid_x1 = center_x - std::max(lid_scale, 10.0);
        double lid_x2 = center_x + std::max(lid_scale, 10.0);
        double lid_y1 = center_y - std::max(lid_scale, 10.0) * std::sin(lid_angle_rad) - lid_height * std::cos(lid_angle_rad);
        double lid_y2 = center_y + std::max(lid_scale, 10.0) * std::sin(lid_angle_rad) - lid_height * std::cos(lid_angle_rad);

        cv::line(img, cv::Point(lid_x1, lid_y1), cv::Point(lid_x2, lid_y2), (waveform_color_scalar + colScalar)/2, 10); // Thickness of eyelid/brow, mix of colors, add knob
        // cv::line(img, cv::Point(lid_x1, lid_y1), cv::Point(lid_x2, lid_y2), colScalar, 9); // Thickness of eyelid/brow

        // Extended polygon to erase upper part
        const int erase_padding = 5;
        int erase_lid_x1 = static_cast<int>(lid_x1) - erase_padding;
        int erase_lid_x2 = static_cast<int>(lid_x2) + erase_padding;
        erase_lid_x1 = std::max(erase_lid_x1, 0);
        erase_lid_x2 = std::min(erase_lid_x2, img.cols - 1);

        std::vector<cv::Point> poly = {
            cv::Point(erase_lid_x1, static_cast<int>(lid_y1)),
            cv::Point(erase_lid_x2, static_cast<int>(lid_y2)),
            cv::Point(erase_lid_x2, 0),
            cv::Point(erase_lid_x1, 0)
        };
        cv::fillConvexPoly(img, poly, cv::Scalar(0, 0, 0));

        publishLedColor(offset_x == 0 ? std::vector<int>{4} : std::vector<int>{12}, eye.color);
    }

    void publishLedColor(const std::vector<int> &led_indices, const std::string &color_hex) {
        auto rgb = hexToRGB(color_hex);
        std_msgs::Int32MultiArray msg;
        for(auto led_idx : led_indices) {
            int color_int = ((led_idx & 0xFF)<<24) | (rgb[2]<<16) | (rgb[1]<<8) | (rgb[0]);
            msg.data.push_back(color_int);
        }
        notification_led_pub_.publish(msg);
    }

    // 2. Modify the renderWaveform function
    void renderWaveform(cv::Mat &img) {
        const int length = 200;       // Width of the waveform area
        const int baseline = 175;     // Y-coordinate baseline for the waveforms

        bool has_audio = !audio_wave_.empty();

        // Generate sine wave once per render cycle
        std::vector<float> sine_wave = generateSineWave(length);
        normalizeWave(sine_wave);

        if (has_audio) {
            
            // --------------------
            // 1. Render Audio Waveform (White, Single Pixel Width)
            // --------------------
            std::vector<float> audio_buf(length, 0.0f);
            updateAudioBuffer(audio_buf);
            normalizeWave(audio_buf);
            /*
            cv::Scalar white_color(255, 255, 255);
            bool first_audio_point = true;
            int prev_y_audio = 0;
            for (int i = 0; i < length; ++i) {
                int x = i;
                int y = static_cast<int>(baseline + (audio_buf[i] * 50.0f / 2.0f));

                if (!first_audio_point) {
                    cv::line(img, cv::Point(x - 1, prev_y_audio), cv::Point(x, y), white_color, 1);
                }
                prev_y_audio = y;
                first_audio_point = false;
            }
            */

            // --------------------
            // 2. Render Combined Waveform (Color-Cycling)
            // --------------------
            std::vector<float> combined_wave(length, 0.0f);
            for (int i = 0; i < length; ++i) {
                combined_wave[i] = 0.25f * sine_wave[i] + 0.75f * audio_buf[i]; // add knob
            }
            normalizeWave(combined_wave);

            bool first_combined_point = true;
            int prev_y_combined = 0;
            for (int i = 0; i < length; ++i) {
                int x = i;
                int y = static_cast<int>(baseline + (combined_wave[i] * 50.0f / 2.0f));

                if (!first_combined_point) {
                    cv::Scalar color = getColorFromPalette(i, length);
                    cv::line(img, cv::Point(x - 1, prev_y_combined), cv::Point(x, y), color, 3); // 3 pixel width, add knob
                }
                prev_y_combined = y;
                first_combined_point = false;
            }
        }

        // --------------------
        // 3. Render Sine Wave
        // --------------------
        bool first_sine_point = true;
        int prev_y_sine = 0;
        cv::Vec3b sine_rgb = hexToRGB(effect_params_.color);
        cv::Scalar sine_color(sine_rgb[0], sine_rgb[1], sine_rgb[2]);

        for (int i = 0; i < length; ++i) {
            int x = i;
            int y = static_cast<int>(baseline + (sine_wave[i] * 50.0f / 2.0f));

            if (!first_sine_point) {
                cv::line(img, cv::Point(x - 1, prev_y_sine), cv::Point(x, y), sine_color, 3); // 3 Pixel width, add knobb
            }
            prev_y_sine = y;
            first_sine_point = false;
        }
    }

    void updateAudioBuffer(std::vector<float> &buffer) {
        if(audio_wave_.empty()) return;
        double elapsed=(ros::Time::now()-audio_start_time_).toSec();
        if(elapsed>audio_duration_) return;
        int needed=(int)buffer.size();
        int samples_passed=(int)(elapsed*audio_sample_rate_);
        int start = std::max(0,samples_passed-needed);
        int end = std::min((int)audio_wave_.size(), start+needed);
        int len = end - start;
        if(len>0) {
            for(int i=0;i<len;i++)
                buffer[i]=audio_wave_[start+i];
        }
    }

    std::vector<float> generateSineWave(int num_samples) {
        std::vector<float> wave(num_samples,0.0f);
        for(int i=0;i<num_samples;i++){
            double t=(double)i/(double)num_samples*2.0*M_PI;
            wave[i] = (float)(effect_params_.amplitude*std::sin(effect_params_.frequency*t+effect_params_.phase));
        }
        effect_params_.phase += effect_params_.phase_increment;
        return wave;
    }

    void normalizeWave(std::vector<float> &wave) {
        if(wave.empty()) return;
        float minv=1e9,maxv=-1e9;
        for(auto v:wave){if(v<minv)minv=v;if(v>maxv)maxv=v;}
        if(minv==maxv){
            for(auto &v:wave) v=0;
            return;
        }
        for(auto &v:wave)
            v=(2.0f*(v-minv)/(maxv-minv)-1.0f)*(float)effect_params_.amplitude;
    }

    std::string imageToAscii(const cv::Mat &img, int cols, int rows) {
            cv::Mat rgba;
            cv::cvtColor(img, rgba, cv::COLOR_BGR2RGBA);

            // 1. Identify which pixels are "pure black" (background)
            cv::Mat black_mask;
            // black_mask will be 255 (white) where the pixel is (0,0,0), and 0 elsewhere
            cv::inRange(img, cv::Scalar(0, 0, 0), cv::Scalar(0, 0, 0), black_mask);

            // 2. Create the Alpha channel
            // We want Alpha=0 (Transparent) where it IS black
            // We want Alpha=255 (Opaque) where it is NOT black
            cv::Mat alpha;
            cv::bitwise_not(black_mask, alpha); // Invert the mask

            // 3. Inject alpha channel into the RGBA image
            std::vector<cv::Mat> channels;
            cv::split(rgba, channels);
            channels[3] = alpha; // Replace the existing alpha (which was all 255)
            cv::merge(channels, rgba);

            caca_canvas_t *cv_canvas = caca_create_canvas(0,0);
            if(!cv_canvas) return "Error: no canvas";
            
            // mask settings match the RGBA layout, which is correct
            caca_dither_t *dither = caca_create_dither(32, rgba.cols, rgba.rows, rgba.step[0],
                                                    0x000000ff,0x0000ff00,0x00ff0000,0xff000000);
            if(!dither) {
                caca_free_canvas(cv_canvas);
                return "Error: no dither";
            }

            caca_set_canvas_size(cv_canvas, cols, rows-1);
            caca_clear_canvas(cv_canvas);
            
            caca_set_dither_antialias(dither, "default"); // add knob
            caca_set_dither_color(dither, "full16"); // add knob
            
            // add knobs
            // ordered2|4|8, random, fstein, none
            caca_set_dither_algorithm(dither, "random"); 

            caca_dither_bitmap(cv_canvas, 0,0, cols, rows-1, dither, rgba.data);

            size_t len;
            void *exported = caca_export_canvas_to_memory(cv_canvas, "ansi", &len);
            std::string output;
            if(exported) {
                output.assign((char*)exported, len);
                free(exported);
            }

            caca_free_dither(dither);
            caca_free_canvas(cv_canvas);

            return output;
        }

    cv::Vec3b hexToRGB(const std::string &hex) {
        int r=0,g=0,b=0;
        if(hex.size()>1 && hex[0]=='#')
            sscanf(hex.c_str()+1,"%02x%02x%02x",&r,&g,&b);
        return cv::Vec3b((uchar)b,(uchar)g,(uchar)r);
    }

    void publishLiveStates() {
        // **Publish Eye Gaze X**
        logos_msgs::EyeGazeX gaze_x_msg;
        gaze_x_msg.eye_side = "both";
        gaze_x_msg.gaze_x = (current_left_.gaze_x + current_right_.gaze_x) * 0.5; 
        gaze_x_msg.duration = 1.0/fps_;
        pub_live_gaze_x_.publish(gaze_x_msg);

        // **Publish Eye Gaze Y**
        logos_msgs::EyeGazeY gaze_y_msg;
        gaze_y_msg.eye_side = "both";
        gaze_y_msg.gaze_y = (current_left_.gaze_y + current_right_.gaze_y) * 0.5;
        gaze_y_msg.duration = 1.0/fps_;
        pub_live_gaze_y_.publish(gaze_y_msg);

        // **Publish Eye Scale X**
        logos_msgs::EyeScaleX scale_x_msg;
        scale_x_msg.eye_side = "both";
        scale_x_msg.scale_x = (current_left_.scale_x + current_right_.scale_x) * 0.5;
        scale_x_msg.duration = 1.0/fps_;
        pub_live_scale_x_.publish(scale_x_msg);

        // **Publish Eye Scale Y**
        logos_msgs::EyeScaleY scale_y_msg;
        scale_y_msg.eye_side = "both";
        scale_y_msg.scale_y = (current_left_.scale_y + current_right_.scale_y) * 0.5;
        scale_y_msg.duration = 1.0/fps_;
        pub_live_scale_y_.publish(scale_y_msg);

        // **Publish Eye Lid Height**
        logos_msgs::EyeLidHeight lid_height_msg;
        lid_height_msg.eye_side = "both";
        lid_height_msg.lid_height = (current_left_.lid_height + current_right_.lid_height) * 0.5;
        lid_height_msg.duration = 1.0/fps_;
        pub_live_lid_height_.publish(lid_height_msg);

        // **Publish Eye Lid Angle**
        logos_msgs::EyeLidAngle lid_angle_msg;
        lid_angle_msg.eye_side = "both";
        lid_angle_msg.lid_angle = (current_left_.lid_angle + current_right_.lid_angle) * 0.5;
        lid_angle_msg.duration = 1.0/fps_;
        pub_live_lid_angle_.publish(lid_angle_msg);

        // **Publish Eye Color**
        logos_msgs::EyeColor color_msg;
        color_msg.eye_side = "both";
        // For color, you might want to handle left and right separately or average them
        // Here, we'll just take the left eye's color as an example
        color_msg.color = current_left_.color;
        color_msg.duration = 1.0/fps_;
        pub_live_color_.publish(color_msg);

        // **Publish Mouth Sine Wave**
        logos_msgs::MouthSine mouth_sine_msg;
        mouth_sine_msg.frequency = effect_params_.frequency;
        mouth_sine_msg.amplitude = effect_params_.amplitude;
        mouth_sine_msg.phase = effect_params_.phase;
        mouth_sine_msg.phase_increment = effect_params_.phase_increment;
        mouth_sine_msg.color = effect_params_.color;
        mouth_sine_msg.duration = 1.0/fps_;
        pub_live_mouth_sine_.publish(mouth_sine_msg);
    }

    // Keyboard handling
    void setupTerminal() {
        tcgetattr(STDIN_FILENO, &orig_settings_);
        termios new_settings=orig_settings_;
        new_settings.c_lflag &= ~(ICANON|ECHO);
        tcsetattr(STDIN_FILENO, TCSANOW, &new_settings);
    }

    void restoreTerminal() {
        tcsetattr(STDIN_FILENO, TCSANOW, &orig_settings_);
    }

    void keypressListener() {
        fd_set set;
        struct timeval timeout;
        while(ros::ok() && !quit_requested_) {
            FD_ZERO(&set);
            FD_SET(STDIN_FILENO,&set);
            timeout.tv_sec=0;timeout.tv_usec=100000;
            int rv=select(STDIN_FILENO+1,&set,NULL,NULL,&timeout);
            if(rv>0) {
                char c;
                if(read(STDIN_FILENO,&c,1)>0) {
                    handleKeyPress(c);
                }
            }
        }
    }

    void handleKeyPress(char key) {
        std::lock_guard<std::mutex> lock(param_mutex_);
        if(key==KEY_QUIT) {
            quit_requested_=true;
        } else if(key==KEY_RESET) {
            // Redetect terminal size
            getTerminalSize();
            printf("\033[2J\033[H");fflush(stdout);
        } else if(key==KEY_INCREASE_COLS) {
            terminal_cols_+=1;
        } else if(key==KEY_DECREASE_COLS) {
            terminal_cols_=std::max(10,terminal_cols_-1);
        } else if(key==KEY_INCREASE_ROWS) {
            terminal_rows_=std::max(10,terminal_rows_-1);
        } else if(key==KEY_DECREASE_ROWS) {
            terminal_rows_+=1;
        } else if(key==KEY_INCREASE_FPS) {
            if(fps_<MAX_FPS_){fps_++;updateRenderTimer();}
        } else if(key==KEY_DECREASE_FPS) {
            if(fps_>MIN_FPS_){fps_--;updateRenderTimer();}
        } else if(key==KEY_CLEAR_SCREEN) {
            printf("\033[2J\033[H");fflush(stdout);
        }
    }
};

int main(int argc, char** argv) {
    ros::init(argc, argv, "logos_face");
    FaceNodeCpp node;
    node.run();
    return 0;
}
