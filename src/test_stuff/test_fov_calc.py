import cv2
import numpy as np
import math

# chessboard settings
CHECKERBOARD = (8, 6)

objp = np.zeros((CHECKERBOARD[0] * CHECKERBOARD[1], 3), np.float32)
objp[:, :2] = np.mgrid[0:CHECKERBOARD[0], 0:CHECKERBOARD[1]].T.reshape(-1, 2)

objpoints = []
imgpoints = []

cap = cv2.VideoCapture(0)

print("Press SPACE to capture calibration images, ESC to finish")

while True:
    ret, frame = cap.read()
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    ret_corners, corners = cv2.findChessboardCorners(gray, CHECKERBOARD)

    if ret_corners:
        cv2.drawChessboardCorners(frame, CHECKERBOARD, corners, ret_corners)
    frame = cv2.flip(frame, -1)
    cv2.imshow("frame", frame)
    key = cv2.waitKey(1)

    if key == 27:  # ESC
        break
    elif key == 32 and ret_corners:
        objpoints.append(objp)
        imgpoints.append(corners)
        print("Captured")

cap.release()
cv2.destroyAllWindows()

ret, mtx, dist, rvecs, tvecs = cv2.calibrateCamera(
    objpoints, imgpoints, gray.shape[::-1], None, None
)

fx = mtx[0, 0]
fy = mtx[1, 1]
width = gray.shape[1]
height = gray.shape[0]

fov_x = 2 * math.degrees(math.atan(width / (2 * fx)))
fov_y = 2 * math.degrees(math.atan(height / (2 * fy)))

print("Horizontal FOV:", fov_x)
print("Vertical FOV:", fov_y)
