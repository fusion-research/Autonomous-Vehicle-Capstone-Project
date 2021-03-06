import numpy as np
from PIL import Image
from sqlitedict import SqliteDict
from distutils.version import StrictVersion
from scipy.spatial.transform import Rotation as rot
 
import cv2
if StrictVersion(cv2.__version__) < StrictVersion('4.2.0'):
    raise ImportError("cv2 version should be 4.2.0 or above")

from utils import rotation_proj, ECC_estimation, feature_matching_estimation

class RadarData:
    
    def __init__(self, ts, img, gps_pos, attitude, precision=0.04):
        self.id = ts
        self.precision = precision
        self.img = img              # array image (0-255)
        self.gps_pos = gps_pos      # 1x3 array
        self.attitude = attitude    # scipy quaternion
        
    def get_img(self):
        """ Return an image of the map (unknown area are set to zero) """ 
        return Image.fromarray(np.nan_to_num(self.img).astype(np.uint8))
        
    def height(self):
        """ Return max y position of a pixel in image frame """
        return self.precision*(np.size(self.img,0)-1)
    
    def width(self):
        """ Return max x position of a pixel in image frame """
        return self.precision*(np.size(self.img,1)-1)
        
    def meters2indices(self, point):
        """ Give the position of a pixel according its position in image frame """
        x_I = int(round(point[0]/self.precision))
        y_I = int(round(point[1]/self.precision))
        return x_I, y_I
    
    def earth2rbd(self,pos, inverse=False):
        """ Change of frame from earth frame to right-backward-down """
        return self.attitude.apply(pos, inverse)
    
    def image_grid(self):
        """ Give the position of each pixel in the image frame """
        x, y = np.meshgrid(np.linspace(0, self.width(), np.size(self.img,1)), np.linspace(0, self.height(), np.size(self.img,0)))
        return np.dstack((x,np.zeros(np.shape(x)),y))
        
    def earth_grid(self):
        """ give the position of each pixel in the earthframe """
        img_grid = self.image_grid()
        earth_grid = self.earth2rbd(img_grid, True) + self.gps_pos
        return np.reshape(earth_grid, np.shape(img_grid))
    
    def image_transformation_from(self, otherdata):
        """ Return the translation and the rotation based on the two radar images """
        translation, rotation = None, None   
        if not (otherdata.id is None or self.id is None):
            cv2_transformations = SqliteDict('cv2_transformations.db')
            if cv2_transformations['use_dataset'] in cv2_transformations:
                if str(self.id)+"-"+str(otherdata.id) in cv2_transformations[cv2_transformations['use_dataset']]:
                    translation, rotation = cv2_transformations[cv2_transformations['use_dataset']][str(self.id)+"-"+str(otherdata.id)]
            cv2_transformations.close()
            
        if translation is None or rotation is None:
            if not otherdata.id is None:        
                print("Calculating transformation: "+ str(self.id)+"-"+str(otherdata.id))
            else:
                print("Calculating transformation: "+ str(self.id))
            try:
                # Restrict to predicted overlap
                self_img, otherdata_img = self.image_overlap(otherdata)
                
                # ECC
                cc, warp_matrix = ECC_estimation(otherdata_img, self_img)
                # ORB
                #warp_matrix = feature_matching_estimation(otherdata_img, self_img, "ORB")
                # SIFT
                #warp_matrix = feature_matching_estimation(otherdata_img, self_img, "SIFT")

                rot_matrix = np.array([[warp_matrix[0,0], warp_matrix[1,0], 0], [warp_matrix[0,1], warp_matrix[1,1], 0], [0,0,1]])
                translation = -self.precision*np.array([warp_matrix[0,2], warp_matrix[1,2], 0])
                rotation = rot.from_dcm(rot_matrix)
            except:   
                print("CV2 calculation failed")
                translation = np.nan
                rotation = np.nan
            
            if not (otherdata.id is None or self.id is None) and not np.any(np.isnan(translation)): 
                cv2_transformations = SqliteDict('cv2_transformations.db', autocommit=True)
                if not cv2_transformations['use_dataset'] in cv2_transformations:
                    d = dict()
                else:
                    d = cv2_transformations[cv2_transformations['use_dataset']]
                d[str(self.id)+"-"+str(otherdata.id)] = (translation, rotation)
                cv2_transformations[cv2_transformations['use_dataset']] = d
                cv2_transformations.close()  
            
        # just for test and vizualisation, could be removed()
        # check_transform(self, rotation, translation, 'radar1_1.png')
            
        return translation, rotation
    
    def image_position_from(self, otherdata):
        """ Return the actual position and attitude based on radar images comparison """
        translation, rotation = self.image_transformation_from(otherdata)
        
        if not np.any(np.isnan(translation)):     
            gps_pos = otherdata.gps_pos + otherdata.earth2rbd(translation,True)
            attitude = rotation.inv()*otherdata.attitude
        else:
            print("No cv2 measurement, use GPS instead")
            trans = otherdata.earth2rbd(self.gps_pos-otherdata.gps_pos)
            trans[2] = 0
            gps_pos = otherdata.gps_pos + otherdata.earth2rbd(trans, True)
            attitude = rotation_proj(otherdata.attitude, self.attitude).inv()*otherdata.attitude
        return gps_pos, attitude
    
    def predict_image(self, gps_pos, attitude, shape=None):
        """ Give the prediction of an observation in a different position based on actual radar image """
        exp_rot_matrix = rotation_proj(attitude, self.attitude).as_dcm()[:2,:2]
        exp_trans = attitude.apply(gps_pos - self.gps_pos)[0:2]/self.precision

        if shape is None:            
            shape = (np.shape(self.img)[1], np.shape(self.img)[0])
        else:
            shape = (shape[1], shape[0])
        
        warp_matrix = np.concatenate((exp_rot_matrix, np.array([[-exp_trans[0]],[-exp_trans[1]]])), axis = 1)
        predict_img = cv2.warpAffine(self.img, warp_matrix, shape, flags=cv2.INTER_LINEAR, borderValue = 0);
        
        mask = cv2.warpAffine(np.ones(np.shape(self.img)), warp_matrix, shape, flags=cv2.INTER_LINEAR, borderValue = 0);
        diff = mask - np.ones((shape[1], shape[0]))
        diff[diff != -1] = 0
        diff[diff == -1] = np.nan
        prediction = diff + predict_img

        return prediction
    
    def image_overlap(self, data2):
        """ Return only the image intersection """
        w1 = np.ones(np.shape(self.img))
        w2 = np.ones(np.shape(data2.img))
    
        white_1 = RadarData(0,w1,self.gps_pos,self.attitude)
        white_2 = RadarData(0,w2,data2.gps_pos,data2.attitude)
    
        mask1 = white_2.predict_image(self.gps_pos,self.attitude)
        mask2 = white_1.predict_image(data2.gps_pos,data2.attitude)
    
        out1 = np.multiply(mask1, self.img)
        out2 = np.multiply(mask2, data2.img)
        return out1.astype(np.uint8), out2.astype(np.uint8)
